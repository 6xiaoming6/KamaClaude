from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.config import ExecutionConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.bash import CommandRule
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class CountingBash(BashTool):
    # 记录真正执行次数，其他行为仍走本机实际 Shell。
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace, ExecutionConfig(backend="host"))
        self.calls = 0

    # 执行真实无副作用的 echo，验证解析只审批而不改写命令。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return await super().invoke(params)


# 功能：验证命令规则贯穿自动允许、人工允许、人工拒绝和明确禁止
# 设计：执行真实 echo 串联，只有批准后才调用 Shell，拒绝时第一段也不能先运行
@pytest.mark.parametrize(("mode", "reply", "expected_calls", "expected_stages"), [
    ("allow", "allow_once", 1, ["executing"]),
    ("ask", "allow_once", 1, ["awaiting_approval", "executing"]),
    ("ask", "deny_once", 0, ["awaiting_approval"]),
    ("deny", "allow_once", 0, []),
])
async def test_command_rules_reach_real_shell(
    tmp_path: Path, mode: str, reply: str, expected_calls: int, expected_stages: list[str],
) -> None:
    rules = [CommandRule(("echo", "first"), "allow")]
    rules.append(CommandRule(("echo", "second"), mode))
    manager = PermissionManager({"bash": ToolPolicy(PermissionDecision.ASK, rules)})
    tool, bus, registry = CountingBash(tmp_path), EventBus(), ToolRegistry()
    registry.register(tool)
    events: list[dict[str, Any]] = []
    stages: list[str] = []

    # 模拟前端接收通知和返回决定，完整经过原有 Future 审批链路。
    async def respond(event: BaseModel) -> None:
        if getattr(event, "type", "") == "permission.requested":
            payload = event.model_dump()
            events.append(payload)
            manager.respond(
                payload["request_id"], reply,
                session_id=payload["session_id"], run_id=payload["run_id"],
            )

    bus.subscribe(respond)
    result = await invoke_tool(
        registry, ToolCallBlock("tool-one", "bash", {"command": "echo first && echo second"}),
        bus, "run-one", permission_manager=manager, session_id="session-one",
        checkpoint_hook=stages.append,
    )
    assert tool.calls == expected_calls
    assert stages == expected_stages
    assert len(events) == (1 if mode == "ask" else 0)
    if events:
        assert events[0]["params"]["command"] == "echo first && echo second"
        assert events[0]["allowed_decisions"] == ["allow_once", "deny_once"]
        assert events[0]["workspace"] == str(tmp_path)
    if expected_calls:
        assert not result.is_error
        assert "first" in result.content and "second" in result.content
    else:
        assert result.error_type == "permission_denied"
    assert not manager._pending and not manager._file_grants and not manager._session_grants


# 功能：验证规则自动允许或等待审批前的检查点失败均不会运行命令
# 设计：在两个前置提交点注入存储错误，检查真实 Shell 调用数与待审批清理
@pytest.mark.parametrize("phase", ["awaiting_approval", "executing"])
async def test_command_checkpoint_failure_stops_execution(tmp_path: Path, phase: str) -> None:
    rule = CommandRule(("echo",), "ask" if phase == "awaiting_approval" else "allow")
    manager = PermissionManager({"bash": ToolPolicy(PermissionDecision.ASK, [rule])})
    tool, registry, bus = CountingBash(tmp_path), ToolRegistry(), EventBus()
    registry.register(tool)

    # 持久化错误必须传播，不能被转换成普通工具失败后继续执行。
    def fail(stage: str) -> None:
        if stage == phase:
            raise CheckpointError("cannot save command intent")

    with pytest.raises(CheckpointError):
        await invoke_tool(
            registry, ToolCallBlock("tool-one", "bash", {"command": "echo never-run"}),
            bus, "run-one", permission_manager=manager, session_id="session-one",
            checkpoint_hook=fail,
        )
    assert tool.calls == 0 and not manager._pending
