from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.permissions.bash import CommandRule
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


# 功能：验证相同 Bash 命令也必须逐次询问，不保留会话批准
# 设计：跨 Run、目录和会话重复请求，并直接尝试所有不支持的持久选项
async def test_command_grants_are_scoped(tmp_path: Path) -> None:
    manager = PermissionManager(timeout_s=0.1)
    events: list[dict[str, Any]] = []
    assert await request(manager, events, workspace=tmp_path) == (True, "allow_once")
    assert await request(manager, events, workspace=tmp_path, run_id="r2") == (True, "allow_once")
    assert len(events) == 2
    await request(manager, events, workspace=tmp_path, command="echo different")
    await request(manager, events, workspace=tmp_path, sid="s2")
    await request(manager, events, workspace=tmp_path / "another")
    await request(PermissionManager(), events, workspace=tmp_path)
    assert len(events) == 6
    assert len({event["request_id"] for event in events}) == 6
    assert all(e["allowed_decisions"] == ["allow_once", "deny_once"] for e in events)
    assert not manager._session_grants and not manager._file_grants


# 功能：验证本次拒绝不禁止后续相同命令
# 设计：重复相同输入，断言第二次仍发布审批且可允许
async def test_session_denial_does_not_ban_other_commands() -> None:
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    assert (await request(manager, events, decision="deny_once"))[0] is False
    assert await request(manager, events) == (True, "allow_once")
    assert len(events) == 2


@pytest.mark.parametrize("tool_name", ["bash", "memory_write", "custom"])
async def test_explicit_deny_wins_over_existing_grants(tool_name: str) -> None:
    manager = PermissionManager({tool_name: ToolPolicy(PermissionDecision.ASK)})
    async def emit(event: dict[str, Any]) -> None:
        reply(manager, event, "allow_once" if tool_name == "bash" else "always_allow")
    params = {"command": "echo hello"} if tool_name == "bash" else {}
    assert (await manager.check_and_wait("t", tool_name, params, "s", emit))[0]
    manager._policies[tool_name] = ToolPolicy(
        PermissionDecision.DENY, command_rules=[CommandRule(("echo",), "allow")],
    )
    assert manager.evaluate(tool_name, params) == PermissionDecision.DENY
    assert await manager.check_and_wait("t", tool_name, params, "s", emit) == (False, "auto_deny")


async def test_deny_pattern_wins_over_grant() -> None:
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    await request(manager, events)
    manager._policies["bash"] = ToolPolicy(
        PermissionDecision.ASK, command_rules=[CommandRule(("echo",), "deny")],
    )
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
    await request(manager, events)
    assert len(events) == 1
    assert path.read_text(encoding="utf-8") == content
    assert manager.evaluate("write_file", {}) == PermissionDecision.DENY
    await request(PermissionManager(policy_file=path), events)
    assert len(events) == 2


# 功能：验证授权仅匹配同一会话、同一工具和同一实际文件
# 设计：改变路径写法、目标、工具、内容和会话，排除原目录级放行及原始路径匹配
async def test_file_grant_is_exact_file_and_tool(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    paths = WorkspacePaths(root)
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
        with pytest.raises(ValueError, match="not allowed"):
            reply(manager, event, "always_deny")
        reply(manager, event, "always_allow" if not event["outside_workspace"] else "allow_once")
    async def write(
        path: str, tool: str = "write_file", *, sid: str = "s", content: str = "x",
    ) -> tuple[bool, str]:
        return await manager.check_and_wait(
            "t", tool, {"path": path, "content": content}, sid, emit, paths.inspect(path),
        )
    await write("a.txt")
    assert events[0]["allowed_decisions"] == ["allow_once", "deny_once", "always_allow"]
    assert str(root / "a.txt") in events[0]["approval_scope"]
    assert await write("./a.txt", content="changed") == (True, "auto_allow")
    assert await write(str(root / "a.txt")) == (True, "auto_allow")
    await write("b.txt")
    await write("nested/c.txt")
    await write("a.txt", "edit_file")
    await write("a.txt", sid="another")
    await write("../outside.txt")
    await write("../outside.txt")
    assert len(events) == 7
    assert all(e["allowed_decisions"] == ["allow_once", "deny_once"] for e in events[-2:])
    manager.cancel_session("s")
    assert all(key[0] != "s" for key in manager._file_grants)
    await write("a.txt")
    assert len(events) == 8


# 功能：验证授权使用后端实际路径而不是模型原始路径，且不跨重启保存
# 设计：故意让原始参数与可信解析结果不同，随后分别访问两个目标
async def test_file_grant_uses_only_backend_target(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    manager = PermissionManager()
    events: list[dict[str, Any]] = []
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
        reply(manager, event, "always_allow")
    async def write(raw: str, target: str) -> tuple[bool, str]:
        return await manager.check_and_wait(
            "t", "write_file", {"path": raw, "content": "x"}, "s", emit,
            paths.inspect(target),
        )
    await write("model.txt", "actual.txt")
    assert events[0]["resolved_path"] == str(tmp_path / "actual.txt")
    assert await write("./actual.txt", "actual.txt") == (True, "auto_allow")
    await write("model.txt", "model.txt")
    assert len(events) == 2
    await write("ACTUAL.txt", "ACTUAL.txt")
    assert len(events) == (2 if os.name == "nt" else 3)
    restarted = PermissionManager()
    async def allow_once(event: dict[str, Any]) -> None:
        events.append(event)
        reply(restarted, event, "allow_once")
    assert await restarted.check_and_wait(
        "t", "write_file", {"path": "actual.txt"}, "s", allow_once, paths.inspect("actual.txt"),
    ) == (True, "allow_once")


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
        for invalid in ("always_allow", "always_deny"):
            with pytest.raises(ValueError, match="not allowed"):
                reply(manager, a, invalid)
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
    await request(permissions, events, sid=session.id)
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
        assert not permissions._session_grants and not permissions._file_grants
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
                event.request_id, "allow_once", session_id=event.session_id, run_id=event.run_id,
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
    assert directories == [str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "b")]


async def test_session_close_wins_over_answer_not_yet_consumed() -> None:
    manager = PermissionManager()
    async def emit(event: dict[str, Any]) -> None:
        reply(manager, event, "allow_once")
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
