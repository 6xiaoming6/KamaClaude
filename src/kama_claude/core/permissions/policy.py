from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kama_claude.core.permissions.bash import CommandRule, command_decision
from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.workspace import PathAccess


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class ToolPolicy:
    default: PermissionDecision
    command_rules: list[CommandRule] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionAssessment:
    decision: PermissionDecision
    reason: str
    reusable: bool = True

# 为一些tool创建默认的策略
DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),
    "write_file": ToolPolicy(default=PermissionDecision.ASK),
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "find_files": ToolPolicy(default=PermissionDecision.ALLOW),
    "search_text": ToolPolicy(default=PermissionDecision.ALLOW),
    "get_file_outline": ToolPolicy(default=PermissionDecision.ALLOW),
    "search_symbol": ToolPolicy(default=PermissionDecision.ALLOW),
    "read_symbol": ToolPolicy(default=PermissionDecision.ALLOW),
    "edit_file": ToolPolicy(default=PermissionDecision.ASK),
    "memory_read": ToolPolicy(default=PermissionDecision.ALLOW),
    "memory_write": ToolPolicy(default=PermissionDecision.ALLOW),
    "search_tool": ToolPolicy(default=PermissionDecision.ALLOW),
    "load_skill": ToolPolicy(default=PermissionDecision.ALLOW),
    "finish": ToolPolicy(default=PermissionDecision.ALLOW),
    "agent_result": ToolPolicy(default=PermissionDecision.ALLOW),
    "agent_cancel": ToolPolicy(default=PermissionDecision.ALLOW),
}

# 未在 DEFAULT_POLICIES 中登记的工具的兜底策略，总是询问用户是否执行
_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK

# bash 参数中展示用的关键字段映射
_PREVIEW_KEY: dict[str, str] = {
    "bash":       "command",
    "read_file":  "path",
    "write_file": "path",
    "list_dir":   "path",
    "find_files": "pattern",
    "search_text": "pattern",
    "edit_file": "path",
    "get_file_outline": "path",
    "search_symbol": "name",
    "read_symbol": "name",
    "memory_read": "key",
    "memory_write": "key",
    "search_tool": "description",
    "load_skill": "name",
    "finish": "result",
}
_PREVIEW_MAX = 60


# 为权限审批事件生成人类可读的参数摘要
def param_preview(tool_name: str, params: dict[str, Any]) -> str:
    key = _PREVIEW_KEY.get(tool_name)
    if key and key in params:
        val = str(params[key])
        if len(val) > _PREVIEW_MAX:
            val = val[:_PREVIEW_MAX] + "…"
        return f"{key}={val!r}"
    snippet = str(params)
    return snippet[:_PREVIEW_MAX] if len(snippet) > _PREVIEW_MAX else snippet


# 查询工具静态策略，不创建审批请求或修改会话授权
def evaluate(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy | None = None,
) -> PermissionDecision:
    return assess(tool_name, params, policy).decision


# 静态规则只在这里判断；Manager 在此结果上处理会话授权和用户等待。
def assess(
    tool_name: str, params: dict[str, Any], policy: ToolPolicy | None = None, *,
    path_access: PathAccess | None = None,
    mode: PermissionMode = PermissionMode.ASK,
    command_platform: str | None = None,
) -> PermissionAssessment:
    policy = policy or DEFAULT_POLICIES.get(tool_name)
    if policy is not None and policy.default == PermissionDecision.DENY:
        return PermissionAssessment(PermissionDecision.DENY, "工具已被明确禁止")
    if tool_name == "bash":
        decision, reason = command_decision(
            str(params.get("command", "")), rules=policy.command_rules if policy else [],
            mode=mode, platform=command_platform,
        )
        return PermissionAssessment(PermissionDecision(decision), reason, reusable=False)
    if mode == PermissionMode.FULL:
        return PermissionAssessment(PermissionDecision.ALLOW, "full 模式：无需审批")
    if mode == PermissionMode.APPROVE:
        if (
            path_access is not None and path_access.outside_workspace
            and tool_name in {"write_file", "edit_file"}
        ):
            return PermissionAssessment(
                PermissionDecision.ASK, "写入工作区外路径", reusable=False,
            )
        return PermissionAssessment(PermissionDecision.ALLOW, "approve 模式：普通操作无需审批")
    if path_access is not None and path_access.outside_workspace:
        return PermissionAssessment(PermissionDecision.ASK, "访问工作区外路径", reusable=False)
    default = policy.default if policy is not None else _UNKNOWN_TOOL_DEFAULT
    return PermissionAssessment(default, "工具默认策略")
