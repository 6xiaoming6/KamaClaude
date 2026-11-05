from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kama_claude.core.workspace import PathAccess


@dataclass(frozen=True)
class ApprovalScope:
    session_id: str
    workspace: str
    tool_name: str
    resource: str


# 授权只在当前会话与工作区内复用，不把命令前缀当作 shell 解析。
def approval_scope(
    session_id: str, workspace: Path, tool_name: str, params: dict[str, Any],
    path_access: PathAccess | None,
) -> tuple[ApprovalScope, str]:
    root = os.path.normcase(str(workspace.resolve()))
    if path_access is not None:
        directory = (
            path_access.target.parent
            if tool_name in {"read_file", "write_file", "edit_file"} else path_access.target
        )
        resource = os.path.normcase(str(directory))
        description = (
            f"本会话 · {tool_name} · 目录 {directory}（不含子目录）"
            if tool_name in {"read_file", "write_file", "edit_file"}
            else f"本会话 · {tool_name} · 固定起始目录 {directory}（按工具参数扫描）"
        )
    elif tool_name == "bash":
        resource = str(params.get("command", ""))
        description = "本会话 · 当前工作目录 · 完全相同的命令"
    else:
        # MCP 工具名带服务前缀；记忆作用域等参数也必须完全相同。
        resource = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        description = f"本会话 · {tool_name} · 完全相同的参数"
    return ApprovalScope(session_id, root, tool_name, resource), description
