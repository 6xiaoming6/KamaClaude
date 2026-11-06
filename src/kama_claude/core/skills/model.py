from __future__ import annotations

import hashlib
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class SkillError(ValueError):
    pass


class SkillEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64, pattern=NAME_PATTERN)
    description: str = Field(min_length=1)


class SkillSnapshot(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=NAME_PATTERN)
    body: str = Field(min_length=1)
    directory: str
    digest: str

    @model_validator(mode="after")
    def check_digest(self) -> Self:
        if self.digest != hashlib.sha256(self.body.encode("utf-8")).hexdigest():
            raise ValueError("invalid skill snapshot digest")
        return self
