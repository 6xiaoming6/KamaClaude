from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kama_claude.core.workspace import PathAccess


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


# 检测 bash 命令是否操作 cwd 之外路径的正则规则列表（强制触发 ASK，不可被 allow_patterns 绕过）
OUTSIDE_CWD_HEURISTICS: list[str] = [
    r"(^|\s)/[^\s]",              # absolute path
    r"(^|\s)~",                   # tilde home
    r"(^|\s)\.\.(/|$|\s)",        # parent traversal
    r"\$\{?HOME\b",               # $HOME variable
    r"\$\{?PWD\b",                # $PWD variable
    r"(^|\s|;|&&|\|\|)cd(\s|$)",  # explicit cd
]

_OUTSIDE_CWD_RE: list[re.Pattern[str]] = [re.compile(p) for p in OUTSIDE_CWD_HEURISTICS]


# 判断 bash 命令是否命中 outside-cwd 启发式规则
def matches_outside_cwd(command: str) -> bool:
    return any(pat.search(command) for pat in _OUTSIDE_CWD_RE)


@dataclass
class ToolPolicy:
    default: PermissionDecision
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


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
    "edit_file": ToolPolicy(default=PermissionDecision.ASK),
    "memory_read": ToolPolicy(default=PermissionDecision.ALLOW),
    "memory_write": ToolPolicy(default=PermissionDecision.ALLOW),
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
    "memory_read": "key",
    "memory_write": "key",
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


# 对工具 + 参数执行 4 层静态策略评估，返回 ALLOW/DENY/ASK
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
) -> PermissionAssessment:
    policy = policy or DEFAULT_POLICIES.get(tool_name)
    if policy is not None and policy.default == PermissionDecision.DENY:
        return PermissionAssessment(PermissionDecision.DENY, "工具已被明确禁止")
    command = str(params.get("command", "")) if tool_name == "bash" else ""
    if command and policy:
        for pattern in policy.deny_patterns:
            if re.search(pattern, command):
                return PermissionAssessment(PermissionDecision.DENY, "命中命令禁止规则")
    if path_access is not None and path_access.outside_workspace:
        return PermissionAssessment(PermissionDecision.ASK, "访问工作区外路径", reusable=False)
    if command and matches_outside_cwd(command):
        return PermissionAssessment(
            PermissionDecision.ASK, "命令可能访问工作区外路径（启发式检查）", reusable=False,
        )
    if tool_name == "memory_write" and params.get("scope", "project") == "global":
        return PermissionAssessment(PermissionDecision.ASK, "写入全局记忆")
    if command and policy:
        for pattern in policy.allow_patterns:
            if re.search(pattern, command):
                return PermissionAssessment(PermissionDecision.ALLOW, "命中命令允许规则")
    default = policy.default if policy is not None else _UNKNOWN_TOOL_DEFAULT
    return PermissionAssessment(default, "工具默认策略")
