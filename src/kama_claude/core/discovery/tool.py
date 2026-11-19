from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, StringConstraints

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.discovery.exposure import load_tools
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class SearchToolCandidatesParams(ToolParams):
    description: str = Field(min_length=1, max_length=2000,
                             description="Describe the required capability for semantic search.")
    keywords: list[Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100,
    )]] = Field(
        min_length=1, max_length=3,
        description="1 to 3 precise terms, e.g. server/tool names or parameter names.",
    )
    topk: int = Field(
        default=3, ge=1, le=10,
        description="Maximum candidates (1-10). Defaults to tool_search.top_k (normally 3). "
                    "Increase when earlier candidates do not fit; refine the query if needed.",
    )


class SearchToolCandidates(BaseTool):
    name = "search_tool_candidates"
    description = (
        "First search for MCP tools needed for the current step. Provide a description for "
        "semantic search and keywords for lexical search (e.g. ['github', 'get_issue']). "
        "The backend adds user task context only to semantic search. Returns names, servers and "
        "short descriptions ONLY, without schemas or tool references. Read the candidates, then "
        "call get_tool_schema(names) on a later model turn for the tools you actually need. "
        "If candidates do not fit, refine description/keywords or increase topk up to 10. "
        "This does not load or execute tools or grant permission."
    )
    params_model = SearchToolCandidatesParams
    input_schema = SearchToolCandidatesParams.model_json_schema()
    retry_safe = True
    timeout_s = 600.0  # 首次库下载和加载权重；仍受整轮 max_run_seconds 限制。

    # records 已按本轮注册表过滤，搜索工具不接触其他会话或白名单外工具。
    def __init__(
        self, run: RunCheckpoint, records: list[CatalogTool], retriever: ToolRetriever,
    ) -> None:
        self._run = run
        self._records = records
        self._retriever = retriever
        # 配置控制省略参数时的默认数量，模型可在单次调用中扩大候选范围。
        self._default_topk = retriever.config.top_k
        schema = SearchToolCandidatesParams.model_json_schema()
        schema["properties"]["topk"]["default"] = self._default_topk
        self.input_schema = schema

    # 只登记检索返回的候选及首次出现步骤，不加载定义或占用 Schema 额度。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        args = SearchToolCandidatesParams.model_validate(params)
        if not args.description.strip():
            return ToolResult("description must contain text", is_error=True,
                              error_type="schema_error")
        result = await self._retriever.search(
            [record for record in self._records if record.public_name in self._run.mcp_tool_names],
            self._run.user_input or self._run.goal, args.description,
            args.keywords,
        )
        topk = args.topk if "topk" in params else self._default_topk
        candidates = result.ranking[:topk]
        records = {record.public_name: record for record in self._records}
        metadata = []
        for name, _ in candidates:
            record = records[name]
            self._run.tool_candidates.setdefault(name, self._run.step)
            metadata.append({
                "name": name, "server": record.server_name,
                "description": str(record.schema["description"])[:240],
                "loaded": name in self._run.loaded_tool_names,
            })
        return ToolResult(json.dumps({
            "method": result.method, "degraded_reason": result.reason,
            "candidates": metadata,
            "message": (
                "Select candidates and call get_tool_schema(names) on your next turn. "
                "If none fit, refine the query or increase topk (max 10)." if metadata
                else "No matching tools; change description or keywords and search again."
            ),
        }, ensure_ascii=False), preserve_in_context=True)


class GetToolSchemaParams(ToolParams):
    names: list[Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=512,
    )]] = Field(
        min_length=1, max_length=10,
        description="1 to 10 exact candidate names with server prefixes; duplicates are ignored.",
    )


class GetToolSchema(BaseTool):
    name = "get_tool_schema"
    description = (
        "Load MCP tools selected from earlier search_tool_candidates results. Pass 1 to 10 exact "
        "names including server prefixes. Returns tool_reference blocks ONLY for newly activated "
        "tools in native deferred mode; otherwise exposes their schemas on the next model turn. "
        "Already active tools are reported without repeating references. Search first, inspect "
        "candidates, then load only what you need. Loaded tools are callable on the NEXT model "
        "turn with normal permissions. After compaction removes a reference it can be loaded again."
    )
    params_model = GetToolSchemaParams
    input_schema = GetToolSchemaParams.model_json_schema()
    retry_safe = True

    # 候选与加载列表都来自同一个 Run，结果与游标由现有批次入口一起提交。
    def __init__(self, run: RunCheckpoint) -> None:
        self._run = run

    # 批量校验候选后只加载未激活工具，当前上下文仍有定义时不重复返回引用。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        names = list(dict.fromkeys(GetToolSchemaParams.model_validate(params).names))
        run = self._run
        outside = [name for name in names if name not in run.mcp_tool_names]
        if outside:
            return ToolResult("Tools outside this Run's allowed MCP catalog: " + ", ".join(outside),
                              is_error=True, error_type="not_found")
        active = set(run.loaded_tool_names)
        pending = [name for name in names if name not in active]
        unsearched = [name for name in pending
                      if name not in run.tool_candidates or run.tool_candidates[name] >= run.step]
        if unsearched:
            return ToolResult(
                "Call search_tool_candidates first and inspect its results on the next model turn "
                "before selecting: " + ", ".join(unsearched),
                is_error=True, error_type="invalid_operation",
            )
        # 列表是检查点中的唯一激活状态；压缩提交时会按保留的引用重新计算。
        selected, rejected = load_tools(run, pending)
        return ToolResult(json.dumps({
            "loaded": selected, "already_loaded": [name for name in names if name in active],
            "not_loaded_budget": rejected,
            "message": (
                "Schema budget exceeded; narrow the request or start a new task." if rejected
                else "Newly loaded tools are available on the next model turn." if selected
                else "Requested tools are already active; no repeated references returned."
            ),
        }, ensure_ascii=False), tool_references=(
            selected if run.tool_loading_mode == "deferred" else []
        ), preserve_in_context=True)
