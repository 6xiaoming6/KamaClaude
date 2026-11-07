from __future__ import annotations

import asyncio
from typing import Any

from textual.widgets import Label

from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.transport.socket_client import IpcError
from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.settings import PermissionModePicker
from kama_claude.tui.widgets.conversation import LLMStreamBlock
from kama_claude.tui.widgets.permissions import PermissionSelect
from tests.unit.test_permission_tui import approval
from tests.unit.test_workspace_tui import idle_socket


def test_mode_change_does_not_split_model_reply() -> None:
    app = KamaTuiApp("127.0.0.1", 1)
    appended: list[Any] = []
    app._view.append = appended.append  # type: ignore[method-assign]
    app._handle_event({"type": "llm.token", "run_id": "r", "token": "hello "})
    app._handle_event({"type": "session.permission_mode_changed", "permission_mode": "approve"})
    app._handle_event({"type": "llm.token", "run_id": "r", "token": "world"})
    blocks = [widget for widget in appended if isinstance(widget, LLMStreamBlock)]
    assert len(blocks) == 1 and blocks[0]._text == "hello world"


class Client:
    def __init__(self) -> None:
        self.mode = "ask"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "session.open":
            return {
                "session": {"id": params["session_id"], "status": "waiting_for_input"},
                "revision": 1, "messages": [], "running": False, "run": None,
                "pending_permissions": [], "permission_mode": self.mode,
            }
        if method == "session.set_permission_mode":
            self.mode = params["mode"]
            return {"permission_mode": self.mode}
        raise AssertionError(method)


async def test_mode_picker_does_not_answer_pending_approval(monkeypatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app, client = KamaTuiApp("127.0.0.1", 1), Client()
    async with app.run_test(size=(120, 50)) as pilot:
        app._client = client  # type: ignore[assignment]
        await app._open_session("sess-one")
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, PermissionModePicker)
        # 模式弹窗打开时收到审批，仍挂载到主界面，不把审批挂进设置页。
        app._handle_event(approval())
        await pilot.pause()
        assert app.screen_stack[0].query(PermissionSelect)
        await pilot.press("y")
        assert len(client.calls) == 1
        app._handle_event({"type": "session.permission_mode_changed", "session_id": "sess-one",
                           "permission_mode": "approve"})
        app._handle_event({"type": "session.waiting_for_input", "session_id": "sess-one"})
        await pilot.press("escape")
        assert not isinstance(app.screen, PermissionModePicker)
        assert "APPROVE" in str(app.query_one("#header", Label).content)
        assert not app._prompt().disabled
        assert len(client.calls) == 1


async def test_mode_picker_and_open_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app, client = KamaTuiApp("127.0.0.1", 1), Client()
    async with app.run_test(size=(120, 45)) as pilot:
        app._client = client  # type: ignore[assignment]
        await app._open_session("sess-one")
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, PermissionModePicker)
        await pilot.click("#mode-approve")
        await pilot.pause()
        assert app._permission_mode == PermissionMode.APPROVE
        assert client.calls[-1] == ("session.set_permission_mode", {"session_id": "sess-one", "mode": "approve"})
        client.mode = "full"
        await app._open_session("sess-other")
        assert app._permission_mode == PermissionMode.FULL
        app._handle_event({"type": "session.permission_mode_changed", "session_id": "sess-one",
                           "permission_mode": "ask"})
        assert app._permission_mode == PermissionMode.FULL


async def test_failed_and_late_mode_response_cannot_overwrite_current_session(monkeypatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app, client = KamaTuiApp("127.0.0.1", 1), Client()
    started, finish = asyncio.Event(), asyncio.Event()
    original = client.send_command
    async def delayed(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session.set_permission_mode":
            started.set()
            await finish.wait()
            return {"permission_mode": "full"}
        return await original(method, params)
    async with app.run_test() as pilot:
        app._client = client  # type: ignore[assignment]
        await app._open_session("sess-one")
        async def failed(method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise IpcError(-32602, "rejected")
        client.send_command = failed
        await app._set_permission_mode(PermissionMode.FULL)
        assert app._permission_mode == PermissionMode.ASK
        client.send_command = delayed
        task = asyncio.create_task(app._set_permission_mode(PermissionMode.FULL))
        await started.wait()
        await app._open_session("sess-other")
        finish.set()
        await task
        assert app._permission_mode == PermissionMode.ASK
        await pilot.pause()
