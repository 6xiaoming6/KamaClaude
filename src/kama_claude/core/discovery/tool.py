from __future__ import annotations

import json

from pydantic import Field

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.discovery.exposure import load_tools
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class SearchToolParams(ToolParams):
    description: str = Field(min_length=1, max_length=2000,
                             description="Describe the capability needed for the current step.")


class SearchTool(BaseTool):
    name = "search_tool"
    description = (
        "Find and load MCP tools needed for the current step. Describe the required capability; "
        "the backend adds the user's task context. Loaded tools become callable on your NEXT "
        "model turn. This only discovers tools; it does not execute them or grant permission."
    )
    params_model = SearchToolParams
    input_schema = SearchToolParams.model_json_schema()
    retry_safe = True
    timeout_s = 600.0  # 首次库下载和加载权重；仍受整轮 max_run_seconds 限制。

    # records 已按本轮注册表过滤，搜索工具不接触其他会话或白名单外工具。
    def __init__(
        self, run: RunCheckpoint, records: list[CatalogTool], retriever: ToolRetriever,
    ) -> None:
        self._run = run
        self._records = records
        self._retriever = retriever

    # 仅修改同一个 Run 的可见列表，由工具批次统一提交结果、历史和游标。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        args = SearchToolParams.model_validate(params)
        if not args.description.strip():
            return ToolResult("description must contain text", is_error=True,
                              error_type="schema_error")
        result = await self._retriever.search(
            [record for record in self._records if record.public_name in self._run.mcp_tool_names],
            self._run.user_input or self._run.goal, args.description,
        )
        candidates = result.ranking[:self._retriever.config.top_k]
        selected, rejected = load_tools(self._run, [name for name, _ in candidates])
        descriptions = {
            record.public_name: record.schema["description"] for record in self._records
        }
        return ToolResult(json.dumps({
            "method": result.method, "degraded_reason": result.reason,
            "loaded": [{"name": name, "description": descriptions[name][:240]}
                       for name in selected],
            "not_loaded_budget": rejected,
            "message": (
                "Schema budget exceeded; narrow the request or start a new task." if rejected
                else "Tools are available on the next model turn." if selected
                else "No matching tools; try a more specific capability or server name."
            ),
        }, ensure_ascii=False), tool_references=(
            selected if self._run.tool_loading_mode == "deferred" else []
        ))
