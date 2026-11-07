from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.bus.commands import SessionSetPermissionModeCommand
from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.events.bus import EventBus
from kama_claude.core.permissions.bash import CommandRule, command_decision
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy, assess
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.workspace import WorkspacePaths


# 模式只改变审批；所有路径都由 WorkspacePaths 解析，禁止规则不会被放宽。
@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize(("tool", "external"), [
    ("read_file", False), ("read_file", True),
    ("write_file", False), ("write_file", True), ("edit_file", True),
])
def test_file_mode_matrix(tmp_path: Path, mode: PermissionMode, tool: str, external: bool) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    access = WorkspacePaths(root).inspect(str((tmp_path if external else root) / "file.txt"))
    # 使用工具入口同样返回的 PathAccess，不使用原始路径判断边界。
    decision = assess(tool, {}, path_access=access, mode=mode).decision
    expected = PermissionDecision.ALLOW
    if mode == PermissionMode.ASK and (external or tool != "read_file"):
        expected = PermissionDecision.ASK
    elif mode == PermissionMode.APPROVE and external and tool != "read_file":
        expected = PermissionDecision.ASK
    assert decision == expected
    assert assess(tool, {}, ToolPolicy(PermissionDecision.DENY), path_access=access,
                  mode=mode).decision == PermissionDecision.DENY


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize(("command", "decision"), [
    ("git status && git diff", "allow"),
    ("echo hello", "allow"),
    ("rm file.txt", "ask"),
    ("del file.txt", "ask"),
    ("rmdir folder", "ask"),
    ("git reset --hard", "ask"),
    ("git -C repo reset --hard", "ask"),
    ("git clean -fd", "ask"),
    ("git branch -D feature", "ask"),
    ("git push --force", "ask"),
    ("python -c dangerous", "ask"),
    ("node script.js", "ask"),
    ("build.cmd", "ask"),
    ("bash script.sh", "ask"),
    ("echo hello > file", "ask"),
])
def test_approve_classifies_commands(platform: str, command: str, decision: str) -> None:
    # 即使普通允许规则匹配，也不能跳过 approve 中的高危检查。
    rules = [CommandRule((command.split()[0],), "allow")]
    assert command_decision(command, rules=rules, platform=platform,
                            mode=PermissionMode.APPROVE)[0] == decision


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("mode", list(PermissionMode))
def test_explicit_forbid_in_every_mode(platform: str, mode: PermissionMode) -> None:
    rules = [CommandRule(("del",), "deny"), CommandRule(("echo",), "allow")]
    for command in ("del file.txt", "echo ok && del file.txt", "del file.txt && echo $DYNAMIC"):
        assert command_decision(command, rules=rules, platform=platform, mode=mode)[0] == "deny"


def test_ask_and_full_and_explicit_ask() -> None:
    for command in ("echo hello", "del file.txt", "echo %PATH%", "python -c dangerous"):
        assert command_decision(command, rules=[], platform="nt", mode=PermissionMode.FULL)[0] == "allow"
    assert command_decision("echo hello", rules=[], mode=PermissionMode.ASK)[0] == "ask"
    assert command_decision("echo hello", rules=[CommandRule(("echo",), "ask")],
                            mode=PermissionMode.APPROVE)[0] == "ask"


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_windows_command_case_cannot_bypass_denial(mode: PermissionMode) -> None:
    rules = [CommandRule(("del",), "deny")]
    assert command_decision("DEL file.txt", rules=rules, platform="nt", mode=mode)[0] == "deny"
    assert not rules[0].matches(["DEL", "file.txt"], platform="posix")


async def test_mode_is_session_local_and_cleared_on_close() -> None:
    manager = PermissionManager()
    manager.set_mode("s1", PermissionMode.FULL)
    async def unexpected(event: dict[str, Any]) -> None:
        pytest.fail("full must not ask")
    assert await manager.check_and_wait("t", "bash", {"command": "del file"}, "s1", unexpected) == (True, "auto_allow")
    assert manager.get_mode("s2") == PermissionMode.ASK
    manager.cancel_session("s1")
    assert manager.get_mode("s1") == PermissionMode.ASK
    assert PermissionManager().get_mode("s1") == PermissionMode.ASK


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_persistent_denial_cannot_be_bypassed(tmp_path: Path, mode: PermissionMode) -> None:
    policy = tmp_path / "permissions.toml"
    policy.write_text('[always]\nmemory_write = "deny"\n', encoding="utf-8")
    manager = PermissionManager(policy_file=policy)
    manager.set_mode("s", mode)
    async def unexpected(event: dict[str, Any]) -> None:
        pytest.fail("explicit denial must not ask")
    assert await manager.check_and_wait("t", "memory_write", {}, "s", unexpected) == (False, "auto_deny")


async def test_mode_change_preserves_explicit_session_denial() -> None:
    manager = PermissionManager()
    async def deny(event: dict[str, Any]) -> None:
        manager.respond(event["request_id"], "always_deny", session_id="s", run_id="r")
    assert await manager.check_and_wait("t", "mcp_custom", {}, "s", deny, run_id="r") == (False, "always_deny")
    async def unexpected(event: dict[str, Any]) -> None:
        pytest.fail("session denial must not ask again")
    for mode in (PermissionMode.APPROVE, PermissionMode.FULL):
        manager.set_mode("s", mode)
        assert await manager.check_and_wait("t", "mcp_custom", {}, "s", unexpected) == (False, "auto_deny")


async def test_changing_mode_does_not_answer_pending_approval() -> None:
    manager = PermissionManager()
    requested: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    async def emit(event: dict[str, Any]) -> None:
        requested.set_result(event)
    task = asyncio.create_task(manager.check_and_wait("t", "bash", {"command": "echo hello"}, "s", emit, run_id="r"))
    try:
        event = await requested
        manager.set_mode("s", PermissionMode.FULL)
        assert not task.done()
        assert manager.pending_for_session("s")[0]["request_id"] == event["request_id"]
        # 等待时新增禁止规则仍会拒绝已收到的允许回答。
        manager._policies["bash"] = ToolPolicy(PermissionDecision.DENY)
        manager.respond(event["request_id"], "allow_once", session_id="s", run_id="r")
        assert await task == (False, "auto_deny")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_session_mode_open_event_and_restart(tmp_path: Path) -> None:
    store, bus, permissions = SessionStore(tmp_path), EventBus(), PermissionManager()
    manager = SessionManager(store, lambda: None, bus, permission_manager=permissions)  # type: ignore[arg-type,return-value]
    events: list[Any] = []
    async def collect(event: Any) -> None:
        events.append(event)
    bus.subscribe(collect)
    session = await manager.create("chat")
    other = await manager.create("chat")
    # 长 Run 持有锁时也可切换模式，不等待 Run 完成。
    async with manager._locks[session.id]:
        await manager.set_permission_mode(session.id, PermissionMode.APPROVE)
    assert (await manager.open(session.id))["permission_mode"] == "approve"
    assert (await manager.open(other.id))["permission_mode"] == "ask"
    assert events[-1].type == "session.permission_mode_changed"
    assert events[-1].session_id == session.id
    assert "permission_mode" not in store.read_meta(session.id).to_dict()
    restarted = SessionManager(store, lambda: None, bus, permission_manager=PermissionManager())  # type: ignore[arg-type,return-value]
    assert (await restarted.open(session.id))["permission_mode"] == "ask"
    await manager.close(session.id)
    assert permissions.get_mode(session.id) == PermissionMode.ASK
    with pytest.raises(HandlerError):
        await manager.set_permission_mode(session.id, PermissionMode.FULL)
    with pytest.raises(HandlerError):
        await manager.set_permission_mode("missing", PermissionMode.FULL)


def test_invalid_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        SessionSetPermissionModeCommand(session_id="s", mode="anything")  # type: ignore[arg-type]
