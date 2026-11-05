from __future__ import annotations

import pytest

from kama_claude.core.transport.socket_client import IpcError
from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.widgets.permissions import PermissionBlock, PermissionSelect
from tests.unit.test_workspace_tui import Client, idle_socket


def approval(request_id: str = "approval-1", run_id: str = "child-a") -> dict:
    return {
        "type": "permission.requested", "request_id": request_id,
        "session_id": "sess-one", "run_id": run_id,
        "tool_use_id": "same-model-id", "tool_name": "bash",
        "params": {"command": "echo " + "x" * 120 + " && echo [last-command]"},
        "param_preview": "short", "workspace": "C:/repo",
        "reason": "存在未匹配允许规则的命令段", "approval_scope": "仅本次操作",
        "allowed_decisions": ["allow_once", "deny_once"],
    }


async def test_full_command_and_concurrent_approval_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client(approval())
    async with app.run_test(size=(120, 65)) as pilot:
        app._client = client
        await app._open_session("sess-one")
        app._handle_event_inner(approval("approval-2", "child-b"))
        app._handle_event_inner(approval("approval-1"))  # 重连重放不能重复创建。
        await pilot.pause()
        assert len(app.query(PermissionSelect)) == 2
        assert len(app.query(PermissionBlock)) == 2
        text = str(app.query(PermissionBlock).first().render())
        assert "[last-command]" in text and "x" * 120 in text
        assert "C:/repo" in text and "未隔离" in text
        assert "授权范围" in text and "审批原因" in text
        app._handle_event_inner({
            "type": "permission.denied", "request_id": "approval-2",
            "tool_use_id": "same-model-id", "decision": "timeout",
        })
        await pilot.pause()
        remaining = app.query_one(PermissionSelect)
        assert remaining._request_id == "approval-1"
        remaining._pick("always_allow")
        await pilot.pause()
        assert client.calls[-1][0] == "session.open"
        remaining._pick("allow_once")
        await pilot.pause()
        assert client.calls[-1][1] == {
            "request_id": "approval-1", "session_id": "sess-one", "run_id": "child-a",
            "decision": "allow_once",
        }


async def test_rejected_rpc_keeps_approval_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client(approval())
    original = client.send_command
    async def reject(method, params):
        if method == "permission.respond":
            raise IpcError(-32602, "invalid approval")
        return await original(method, params)
    client.send_command = reject
    async with app.run_test(size=(120, 55)) as pilot:
        app._client = client
        await app._open_session("sess-one")
        await pilot.pause()
        app.query_one(PermissionSelect)._pick("allow_once")
        await pilot.pause()
        assert len(app.query(PermissionSelect)) == 1
        assert "approval-1" in app._pending_permission_blocks
        assert not app.query_one(PermissionBlock)._resolved
