from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from kama_claude.core.workspace import PathAccess, WorkspacePaths


class ToolParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # 错误类型与副作用确定性分开：业务失败不等于结果未知
    error_type: str | None = None
    outcome_unknown: bool = False
    # 发现工具的结构化引用；content 继续用于日志、事件和界面展示。
    tool_references: list[str] = field(default_factory=list)
    preserve_in_context: bool = False  # 有独立预算的流程说明，不能再裁成头尾预览。

    def message_content(self) -> str | list[dict[str, object]]:
        if not self.tool_references or self.is_error:
            return self.content
        blocks: list[dict[str, object]] = [{"type": "text", "text": self.content}]
        blocks.extend({"type": "tool_reference", "tool_name": name}
                      for name in self.tool_references)
        return blocks


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    retry_safe: ClassVar[bool] = False
    timeout_s: ClassVar[float] = 120.0
    params_model: ClassVar[type[BaseModel] | None] = None

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...


class FileTool(BaseTool):
    # 所有通用文件工具共用同一个根目录与路径规则
    def __init__(self, workspace: Path | None = None) -> None:
        self.paths = WorkspacePaths(workspace)

    # 提供真实路径供权限层判断，调用入口已先完成参数模型校验
    def path_access(self, params: dict[str, object]) -> PathAccess:
        access = self.paths.inspect(str(params.get("path", ".")))
        self._check_writable(access.target)
        return access

    # 审批前和真实写入前都检查；直接调用不能跳过用户规则只读保护。
    def _check_writable(self, path: Path) -> None:
        if self.name in {"write_file", "edit_file"}:
            from kama_claude.core.memory.paths import guard_file_write
            from kama_claude.core.workspace import WorkspacePathError

            try:
                guard_file_write(path, self.paths.root)
            except PermissionError as exc:
                raise WorkspacePathError(str(exc)) from exc

    # 直接调用同样校验边界；approved_path 是内部参数，不属于工具 Schema
    async def invoke(
        self, params: dict[str, object], *, approved_path: Path | None = None,
    ) -> ToolResult:
        if self.params_model is not None:
            self.params_model.model_validate(params)
        path = self.paths.resolve(str(params.get("path", ".")), approved_path=approved_path)
        self._check_writable(path)
        return await self._invoke_path(params, path)

    # 子类只处理已通过边界校验的文件操作
    @abstractmethod
    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult: ...
