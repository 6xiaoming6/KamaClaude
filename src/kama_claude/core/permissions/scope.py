from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ApprovalScope:
    session_id: str
    workspace: str
    tool_name: str
    resource: str


# Task、记忆与 MCP 继续按会话、工作区和完整参数匹配，文件与 Bash 不走这里。
def approval_scope(
    session_id: str, workspace: Path, tool_name: str, params: dict[str, Any],
) -> tuple[ApprovalScope, str]:
    root = os.path.normcase(str(workspace.resolve()))
    # MCP 工具名带服务前缀；记忆作用域等参数也必须完全相同。
    resource = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    description = f"本会话 · {tool_name} · 完全相同的参数"
    return ApprovalScope(session_id, root, tool_name, resource), description
