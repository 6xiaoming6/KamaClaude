from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

type MemoryScope = Literal["global", "project"]
type MemoryType = Literal["user", "feedback", "project", "reference"]


class MemoryError(ValueError):
    # 明确区分权限、冲突与格式错误，调用方不用匹配报错文本。
    def __init__(self, message: str, error_type: str = "invalid_operation") -> None:
        super().__init__(message)
        self.error_type = error_type


class MemoryEntry(BaseModel):
    key: str
    summary: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1)
    type: MemoryType = "project"
    updated_at: str
    source: str


class MemorySource(BaseModel):
    kind: str
    path: str
    digest: str


class MemorySnapshot(BaseModel):
    text: str = ""
    estimated_tokens: int = 0
    sources: list[MemorySource] = Field(default_factory=list)
