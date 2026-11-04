from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

type MemoryScope = Literal["global", "project", "session"]
SCOPES: tuple[MemoryScope, ...] = ("global", "project", "session")


class MemoryError(ValueError):
    pass


class MemoryEntry(BaseModel):
    key: str
    summary: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1)
    pinned: bool = False
    updated_at: str
    source: str


class MemoryLayerSnapshot(BaseModel):
    scope: MemoryScope
    budget: int
    estimated_tokens: int = 0
    included: list[str] = Field(default_factory=list)
    summarized: list[str] = Field(default_factory=list)
    omitted: int = 0
    warnings: list[str] = Field(default_factory=list)


class MemorySnapshot(BaseModel):
    text: str = ""
    estimated_tokens: int = 0
    layers: list[MemoryLayerSnapshot] = Field(default_factory=list)
