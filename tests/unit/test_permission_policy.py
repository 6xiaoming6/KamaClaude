from __future__ import annotations

import pytest

from kama_claude.core.permissions.bash import CommandRule
from kama_claude.core.permissions.policy import (
    PermissionDecision,
    ToolPolicy,
    evaluate,
    param_preview,
)


# 功能：验证普通工具默认策略不受命令规则调整影响
# 设计：读取默认允许、写入默认询问，未知工具与全局记忆仍保守询问
def test_tool_defaults() -> None:
    for tool in ("read_file", "list_dir", "find_files", "search_text", "memory_write"):
        assert evaluate(tool, {}) == PermissionDecision.ALLOW
    for tool in ("write_file", "edit_file", "some_future_tool", "bash"):
        assert evaluate(tool, {}) == PermissionDecision.ASK
    assert evaluate("memory_write", {"scope": "global"}) == PermissionDecision.ASK


# 功能：验证工具明确禁止优先于参数允许规则
# 设计：同时配置工具 deny 与匹配的命令 allow，结果必须是拒绝
def test_explicit_deny_wins() -> None:
    policy = ToolPolicy(
        PermissionDecision.DENY, command_rules=[CommandRule(("echo",), "allow")],
    )
    assert evaluate("bash", {"command": "echo hello"}, policy) == PermissionDecision.DENY


# 功能：验证静态入口实际调用参数规则，不再匹配整段命令正则
# 设计：同一程序不同参数与参数中的程序名应得到不同结果
@pytest.mark.parametrize(("command", "expected"), [
    ("git status", PermissionDecision.ALLOW),
    ("git status --short", PermissionDecision.ALLOW),
    ("git push", PermissionDecision.ASK),
    ("echo git status", PermissionDecision.ASK),
    ("git status && git push --force", PermissionDecision.DENY),
])
def test_bash_uses_argument_rules(command: str, expected: PermissionDecision) -> None:
    policy = ToolPolicy(PermissionDecision.ASK, command_rules=[
        CommandRule(("git", "status"), "allow"),
        CommandRule(("git", "push", "--force"), "deny"),
    ])
    assert evaluate("bash", {"command": command}, policy) == expected


# 功能：验证默认 allow 不能替代明确的命令允许规则
# 设计：空规则下分别使用 ASK 和 ALLOW 默认值，Bash 均不得自动执行
@pytest.mark.parametrize("default", [PermissionDecision.ASK, PermissionDecision.ALLOW])
def test_bash_without_rules_asks(default: PermissionDecision) -> None:
    assert evaluate("bash", {"command": "echo hello"}, ToolPolicy(default)) == PermissionDecision.ASK


# 功能：验证命令规则不会作用于普通文件工具
# 设计：写入工具带冲突命令规则时仍只按文件默认策略和路径判断
def test_command_rules_only_apply_to_bash() -> None:
    policy = ToolPolicy(PermissionDecision.ASK, command_rules=[
        CommandRule(("write_file",), "allow"), CommandRule(("write_file",), "deny"),
    ])
    assert evaluate("write_file", {"path": "x.txt", "content": "hi"}, policy) == PermissionDecision.ASK


# 功能：验证审批摘要格式和截断行为保持稳定
# 设计：摘要可以截短，前端另行展示完整命令和实际路径
def test_param_preview() -> None:
    assert param_preview("bash", {"command": "echo hi"}) == "command='echo hi'"
    assert param_preview("read_file", {"path": "README.md"}) == "path='README.md'"
    assert param_preview("memory_write", {"key": "python"}) == "key='python'"
    preview = param_preview("bash", {"command": "echo " + "x" * 100})
    assert len(preview) <= 75 and "…" in preview
