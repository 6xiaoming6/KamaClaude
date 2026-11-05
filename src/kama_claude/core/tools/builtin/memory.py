from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import Field, model_validator

from kama_claude.core.memory.model import MemoryError, MemoryScope, MemoryType
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class MemoryReadParams(ToolParams):
    scope: MemoryScope = "project"
    key: str | None = None
    query: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    # 全局入口是单个自动文件，不能通过 key 选择用户规则或其他文件。
    def check_global(self) -> Self:
        if self.scope == "global" and (self.key is not None or self.query):
            raise ValueError("global reads do not accept key or query")
        return self


class MemoryWriteParams(ToolParams):
    scope: MemoryScope = "project"
    key: str | None = None
    action: Literal["upsert", "delete"] = "upsert"
    type: MemoryType = "project"
    summary: str | None = Field(default=None, min_length=1, max_length=160)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    # 项目更新按 key，更新全局自动文件必须携带先前读取的 hash。
    def check_target(self) -> Self:
        if self.scope == "global":
            if self.key is not None or self.summary is not None or self.expected_hash is None:
                raise ValueError(
                    "global writes require expected_hash and do not accept key or summary",
                )
        elif self.key is None or self.expected_hash is not None:
            raise ValueError("project writes require key and do not accept expected_hash")
        if self.action == "upsert" and (
            self.content is None or (self.scope == "project" and self.summary is None)
        ):
            raise ValueError("upsert requires content and project upsert also requires summary")
        return self


class MemoryReadTool(BaseTool):
    name = "memory_read"
    description = (
        "Read Agent-maintained memory. Project: list/search the index without key, or read a "
        "topic by key. Global: read AGENTS.auto.md and its expected_hash, no key. "
        "Use next_offset for body chunks of at most 4000 characters. Never changes user rules."
    )
    retry_safe = True
    params_model = MemoryReadParams
    input_schema = MemoryReadParams.model_json_schema()

    # 存储目录和项目归属由后端绑定，不接受模型路径。
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # 详细主题作为普通工具结果返回，不改写当前冻结提示。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MemoryReadParams.model_validate(params)
        try:
            if p.scope == "global":
                result = self._store.read_global(offset=p.offset)
            elif p.key is not None:
                result = self._store.read(p.key, offset=p.offset)
            else:
                result = self._store.list(query=p.query, offset=p.offset, limit=p.limit)
        except MemoryError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
        return ToolResult(content=json.dumps(result, ensure_ascii=False))


class MemoryWriteTool(BaseTool):
    name = "memory_write"
    description = (
        "Maintain concise Agent-owned Markdown memory, never user AGENTS.md. "
        "Project: upsert/delete key, with type user/feedback/project/reference; provide a one-line "
        "summary and detailed content for upsert. The index is maintained automatically. "
        "Global: replace/delete AGENTS.auto.md using expected_hash from memory_read, no key. "
        "Remember durable preferences, corrections and decisions, not logs, secrets or guesses."
    )
    retry_safe = True
    params_model = MemoryWriteParams
    input_schema = MemoryWriteParams.model_json_schema()

    # 来源由 Core 生成，模型不能冒充其他会话或 Run。
    def __init__(self, store: MemoryStore, session_id: str, run_id: str) -> None:
        self._store = store
        self._source = f"{session_id or 'standalone'} / {run_id}"

    # 先校验预算再提交；索引部分失败和全局冲突都返回明确错误。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MemoryWriteParams.model_validate(params)
        try:
            if p.scope == "global":
                assert p.expected_hash is not None
                result = self._store.write_global(
                    None if p.action == "delete" else p.content, p.expected_hash,
                )
            else:
                assert p.key is not None
                if p.action == "delete":
                    result = self._store.delete(p.key)
                else:
                    assert p.summary is not None and p.content is not None
                    result = self._store.upsert(
                        p.key, summary=p.summary, content=p.content, type=p.type,
                        source=self._source,
                    )
        except MemoryError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
        return ToolResult(content=json.dumps(result, ensure_ascii=False))
