from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.workspace import WorkspacePaths


def reply(manager: PermissionManager, event: dict[str, Any], decision: str) -> None:
    manager.respond(
        event["request_id"], decision, session_id=event["session_id"], run_id=event["run_id"],
    )


async def request(
    manager: PermissionManager, events: list[dict[str, Any]], *, decision: str = "allow_once",
    command: str = "echo hello", sid: str = "s1", run_id: str = "r1",
    workspace: Path | None = None,
) -> tuple[bool, str]:
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
        reply(manager, event, decision)

    return await manager.check_and_wait(
        "same-model-tool-id", "bash", {"command": command}, sid, emit,
        run_id=run_id, workspace=workspace,
    )


# 同一会话的同一命令复用，换命令、会话、工作区或重启都不能复用。
async def test_command_grants_are_scoped(tmp_path: Path) -> None:
    manager = PermissionManager(timeout_s=0.1)
    events: list[dict[str, Any]] = []
    assert await request(manager, events, decision="always_allow", workspace=tmp_path) == (
        True, "always_allow",
    )
    assert await request(manager, events, workspace=tmp_path, run_id="r2") == (True, "auto_allow")
    assert len(events) == 1
    await request(manager, events, workspace=tmp_path, command="echo different")
    await request(manager, events, workspace=tmp_path, sid="s2")
    await request(manager, events, workspace=tmp_path / "another")
    await request(PermissionManager(), events, workspace=tmp_path)
    assert len(events) == 5
    assert len({event["request_id"] for event in events}) == 5


async def test_session_denial_does_not_ban_other_commands() -> None:
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    assert (await request(manager, events, decision="always_deny"))[0] is False
    assert await request(manager, events) == (False, "auto_deny")
    assert (await request(manager, events, command="echo other"))[0] is True
    assert len(events) == 2


@pytest.mark.parametrize("tool_name", ["bash", "memory_write", "custom"])
async def test_explicit_deny_wins_over_existing_grants(tool_name: str) -> None:
    manager = PermissionManager({tool_name: ToolPolicy(PermissionDecision.ASK)})
    async def emit(event: dict[str, Any]) -> None:
        reply(manager, event, "always_allow")
    params = {"command": "echo hello"} if tool_name == "bash" else {}
    assert (await manager.check_and_wait("t", tool_name, params, "s", emit))[0]
    manager._policies[tool_name] = ToolPolicy(PermissionDecision.DENY, allow_patterns=[".*"])
    assert manager.evaluate(tool_name, params) == PermissionDecision.DENY
    assert await manager.check_and_wait("t", tool_name, params, "s", emit) == (False, "auto_deny")


async def test_deny_pattern_wins_over_grant() -> None:
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    await request(manager, events, decision="always_allow")
    manager._policies["bash"] = ToolPolicy(PermissionDecision.ASK, deny_patterns=["echo"])
    assert await request(manager, events) == (False, "auto_deny")
    assert len(events) == 1


async def test_default_allow_has_no_prompt() -> None:
    manager = PermissionManager()
    async def unexpected(event: dict[str, Any]) -> None:
        pytest.fail("unexpected approval")
    assert manager.evaluate("read_file", {}) == PermissionDecision.ALLOW
    assert await manager.check_and_wait("t", "read_file", {}, "s", unexpected) == (True, "auto_allow")


async def test_legacy_allow_is_ignored_deny_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    content = '[always]\nbash = "allow"\nwrite_file = "deny"\n'
    path.write_text(content, encoding="utf-8")
    manager = PermissionManager(policy_file=path)
    events: list[dict[str, Any]] = []
    await request(manager, events, decision="always_allow")
    assert len(events) == 1
    assert path.read_text(encoding="utf-8") == content
    assert manager.evaluate("write_file", {}) == PermissionDecision.DENY
    await request(PermissionManager(policy_file=path), events)
    assert len(events) == 2


async def test_file_grant_is_exact_directory_and_tool(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    paths = WorkspacePaths(root)
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
        reply(manager, event, "always_allow" if not event["outside_workspace"] else "allow_once")
    async def write(path: str, tool: str = "write_file") -> tuple[bool, str]:
        return await manager.check_and_wait(
            "t", tool, {"path": path, "content": "x"}, "s", emit, paths.inspect(path),
        )
    await write("a.txt")
    assert await write("b.txt") == (True, "auto_allow")
    await write("nested/c.txt")
    await write("a.txt", "edit_file")
    await write("../outside.txt")
    await write("../outside.txt")
    assert len(events) == 5
    assert all(e["allowed_decisions"] == ["allow_once", "deny_once"] for e in events[-2:])


@pytest.mark.parametrize("command", ["cat /etc/hosts", "cd .."])
async def test_external_command_only_allows_once(command: str) -> None:
    events: list[dict[str, Any]] = []
    manager = PermissionManager({"bash": ToolPolicy(PermissionDecision.ALLOW)})
    await request(manager, events, command=command)
    await request(manager, events, command=command)
    assert len(events) == 2 and not manager._session_grants
    assert events[0]["allowed_decisions"] == ["allow_once", "deny_once"]


# 两个并行子 Run 即使拿到相同模型工具 ID，审批也互不覆盖。
async def test_concurrent_ids_binding_replay_and_invalid_decisions() -> None:
    manager = PermissionManager(timeout_s=0)
    async def emit(event: dict[str, Any]) -> None:
        pass
    tasks = [asyncio.create_task(manager.check_and_wait(
        "same", "bash", {"command": "echo hello"}, "s", emit, run_id=rid,
    )) for rid in ("child-a", "child-b")]
    try:
        await asyncio.sleep(0)
        a, b = manager.pending_for_session("s")
        assert a["request_id"] != b["request_id"]
        for sid, rid in (("other", "child-a"), ("s", "wrong")):
            with pytest.raises(ValueError, match="belong"):
                manager.respond(a["request_id"], "allow_once", session_id=sid, run_id=rid)
        with pytest.raises(ValueError, match="not allowed"):
            reply(manager, a, "invented")
        assert len(manager.pending_for_session("s")) == 2
        reply(manager, b, "deny_once")
        assert await tasks[1] == (False, "deny_once")
        assert not tasks[0].done()
        with pytest.raises(ValueError, match="expired"):
            reply(manager, b, "allow_once")
        reply(manager, a, "allow_once")
        assert await tasks[0] == (True, "allow_once")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not manager._pending


async def test_timeout_rejects_late_reply_and_cannot_approve_new_attempt() -> None:
    manager = PermissionManager(timeout_s=0.01)
    events: list[dict[str, Any]] = []
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
    assert await manager.check_and_wait("same", "bash", {}, "s", emit) == (False, "timeout")
    with pytest.raises(ValueError, match="expired"):
        reply(manager, events[0], "allow_once")
    task = asyncio.create_task(manager.check_and_wait("same", "bash", {}, "s", emit))
    await asyncio.sleep(0)
    assert events[0]["request_id"] != events[1]["request_id"]
    with pytest.raises(ValueError):
        reply(manager, events[0], "allow_once")
    reply(manager, events[1], "allow_once")
    assert (await task)[0]


async def test_emitter_failure_and_cancellation_cleanup() -> None:
    manager = PermissionManager(timeout_s=0)
    async def fail(event: dict[str, Any]) -> None:
        raise RuntimeError("observer failed")
    with pytest.raises(RuntimeError, match="observer"):
        await manager.check_and_wait("t", "bash", {}, "s", fail)
    assert not manager._pending
    async def emit(event: dict[str, Any]) -> None:
        pass
    tasks = [asyncio.create_task(manager.check_and_wait("t", "bash", {}, sid, emit)) for sid in ("a", "b")]
    await asyncio.sleep(0)
    manager.cancel_session("a")
    assert not (await tasks[0])[0]
    assert not tasks[1].done()
    tasks[1].cancel()
    await asyncio.gather(tasks[1], return_exceptions=True)
    assert not manager._pending


async def test_reconnect_snapshot_cannot_mutate_bound_request() -> None:
    manager = PermissionManager()
    async def emit(event: dict[str, Any]) -> None:
        event["params"]["command"] = "mutated"
        snapshot = manager.pending_for_session("s")[0]
        assert snapshot["params"]["command"] == "echo original"
        snapshot["params"]["command"] = "changed"
        fresh = manager.pending_for_session("s")[0]
        assert fresh["params"]["command"] == "echo original"
        reply(manager, fresh, "allow_once")
    assert (await manager.check_and_wait("t", "bash", {"command": "echo original"}, "s", emit))[0]


async def test_no_session_cannot_grant_reusable_permission() -> None:
    manager = PermissionManager()
    async def emit(event: dict[str, Any]) -> None:
        assert event["allowed_decisions"] == ["allow_once", "deny_once"]
        with pytest.raises(ValueError):
            reply(manager, event, "always_allow")
        reply(manager, event, "allow_once")
    assert (await manager.check_and_wait("t", "bash", {}, "", emit))[0]


async def test_open_preserves_child_approval_identity_and_close_revokes(tmp_path: Path) -> None:
    from kama_claude.core.session.store import SessionStore
    from tests.unit.test_checkpoint_recovery import Provider
    from tests.unit.test_checkpoint_recovery import manager as session_manager

    permissions = PermissionManager(timeout_s=0)
    sessions = session_manager(SessionStore(tmp_path), Provider([]), [], permissions=permissions)
    session = await sessions.create("chat")
    events: list[dict[str, Any]] = []
    await request(permissions, events, sid=session.id, decision="always_allow")
    async def emit(event: dict[str, Any]) -> None:
        pass
    pending = asyncio.create_task(permissions.check_and_wait(
        "same", "bash", {"command": "echo other"}, session.id, emit, run_id="child-run",
    ))
    await asyncio.sleep(0)
    try:
        first = (await sessions.open(session.id))["pending_permissions"][0]
        second = (await sessions.open(session.id))["pending_permissions"][0]
        assert first == second and first["run_id"] == "child-run"
        await sessions.close(session.id)
        assert await pending == (False, "deny_once")
        assert not permissions._session_grants
        with pytest.raises(ValueError, match="expired"):
            reply(permissions, first, "allow_once")
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_mcp_grant_matches_server_tool_and_normalized_arguments() -> None:
    permissions = PermissionManager()
    events: list[dict[str, Any]] = []
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
        reply(permissions, event, "always_allow")
    async def call(name: str, params: dict[str, Any]) -> tuple[bool, str]:
        return await permissions.check_and_wait("same", name, params, "s", emit)
    await call("server_a__lookup", {"query": "hello", "limit": 1})
    assert await call("server_a__lookup", {"limit": 1, "query": "hello"}) == (True, "auto_allow")
    await call("server_a__lookup", {"query": "other", "limit": 1})
    await call("server_b__lookup", {"query": "hello", "limit": 1})
    assert len(events) == 3


async def test_bash_approval_uses_actual_tool_directory(tmp_path: Path) -> None:
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.types import ToolCallBlock
    from kama_claude.core.tools.builtin.bash import BashTool
    from kama_claude.core.tools.invocation import invoke_tool
    from kama_claude.core.tools.registry import ToolRegistry

    permissions, bus = PermissionManager(), EventBus()
    directories = []
    async def emit(event: Any) -> None:
        if event.type == "permission.requested":
            directories.append(event.workspace)
            permissions.respond(
                event.request_id, "always_allow", session_id=event.session_id, run_id=event.run_id,
            )
    bus.subscribe(emit)
    for name in ("a", "b", "b"):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        registry = ToolRegistry()
        registry.register(BashTool(root))
        result = await invoke_tool(
            registry, ToolCallBlock("same", "bash", {"command": "echo hello"}), bus, "r",
            session_id="s", permission_manager=permissions, workspace=tmp_path,
        )
        assert not result.is_error
    assert directories == [str(tmp_path / "a"), str(tmp_path / "b")]


async def test_session_close_wins_over_answer_not_yet_consumed() -> None:
    manager = PermissionManager()
    async def emit(event: dict[str, Any]) -> None:
        reply(manager, event, "always_allow")
        manager.cancel_session("s", reason="session_closed")
    assert await manager.check_and_wait("t", "bash", {}, "s", emit) == (False, "deny_once")
    assert not manager._pending and not manager._session_grants


async def test_rpc_does_not_report_success_without_permission_manager() -> None:
    from kama_claude.core.app import CoreApp
    from kama_claude.core.bus.envelope import HandlerError

    with pytest.raises(HandlerError):
        await CoreApp()._permission_respond_handler({
            "request_id": "stale", "session_id": "s", "run_id": "r", "decision": "allow_once",
        })
