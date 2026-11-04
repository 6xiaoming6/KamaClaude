from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import Field, model_validator

from kama_claude.core.memory.model import MemoryError, MemoryScope
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult


class MemoryReadParams(ToolParams):
    scope: MemoryScope = "project"
    key: str | None = None
    query: str = ""
    offset: int = Field(default=0, ge=0, description="List item offset, or body character offset.")
    limit: int = Field(
        default=20, ge=1, le=50, description="Maximum list items; body chunks are 4000 chars.",
    )


class MemoryWriteParams(ToolParams):
    scope: MemoryScope = "project"
    key: str
    action: Literal["upsert", "delete"] = "upsert"
    summary: str | None = Field(default=None, min_length=1, max_length=160)
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    pinned: bool | None = None

    @model_validator(mode="after")
    # 更新必须给出摘要和正文，删除只需要 scope 与 key
    def require_content(self) -> Self:
        if self.action == "upsert" and (self.summary is None or self.content is None):
            raise ValueError("upsert requires summary and content")
        return self


class MemoryReadTool(BaseTool):
    name = "memory_read"
    description = (
        "List Markdown memory summaries, find exact keywords, or read a key's body. "
        "Scopes: global preferences, project facts, session decisions. "
        "Use returned next_offset to continue; reading does not create or change files."
    )
    retry_safe = True
    params_model = MemoryReadParams
    input_schema = MemoryReadParams.model_json_schema()

    # 注入固定目录的存储，工具不能选择任意文件路径
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # 无 key 时列索引，有 key 时按需读取正文
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = MemoryReadParams.model_validate(params)
        try:
            result = (
                self._store.read(parsed.scope, parsed.key, offset=parsed.offset)
                if parsed.key is not None else self._store.list(
                    parsed.scope, query=parsed.query, offset=parsed.offset, limit=parsed.limit,
                )
            )
        except MemoryError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
        return ToolResult(content=json.dumps(result, ensure_ascii=False))


class MemoryWriteTool(BaseTool):
    name = "memory_write"
    description = (
        "Upsert or delete a keyed Markdown memory. Reuse the same key when a fact changes. "
        "Default scope is project; use session for temporary decisions. "
        "Global writes require explicit user intent and permission approval. "
        "Do not store uncertain outcomes as facts. Pin only small, frequently needed facts."
    )
    retry_safe = True
    params_model = MemoryWriteParams
    input_schema = MemoryWriteParams.model_json_schema()

    # 来源由 Core 绑定，不能让模型伪造其他会话或 Run 身份
    def __init__(self, store: MemoryStore, session_id: str, run_id: str) -> None:
        self._store = store
        self._source = f"{session_id or 'standalone'} / {run_id}"

    # 写入与删除都是按 key 的幂等操作；格式或磁盘错误作为明确工具错误返回
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = MemoryWriteParams.model_validate(params)
        try:
            if parsed.action == "delete":
                result = self._store.delete(parsed.scope, parsed.key)
            else:
                assert parsed.summary is not None and parsed.content is not None
                result = self._store.upsert(
                    parsed.scope, parsed.key, summary=parsed.summary, content=parsed.content,
                    pinned=parsed.pinned, source=self._source,
                )
        except MemoryError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
        return ToolResult(content=json.dumps(result, ensure_ascii=False))
