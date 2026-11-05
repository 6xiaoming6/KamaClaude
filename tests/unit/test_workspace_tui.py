from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual import events
from textual.widgets import Button

from kama_claude.tui.app import KamaTuiApp, PermissionBlock, PermissionSelect


# 保留 Textual 生命周期，避免测试建立外部连接
async def idle_socket(self: KamaTuiApp) -> None:
    await asyncio.Future()


# 返回打开会话的快照并记录真实前端发送的审批
class Client:
    # 保存重连时尚未处理的审批请求
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = {
            "request_id": "approval-one", "session_id": "sess-one", "run_id": "run-one",
            **request,
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # 用同一待审批载荷模拟 session.open 和 permission.respond
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "session.open":
            return {
                "session": {"id": "sess-one", "status": "active", "mode": "chat"},
                "revision": 2, "messages": [], "running": True, "run": None,
                "pending_permissions": [self.request], "event_seq": 0,
            }
        assert method == "permission.respond"
        return {"ok": True}


# 功能：验证重连审批完整展示外部路径，只提供单次选项且过滤所有旧快捷键
# 设计：真实打开历史快照，在控件有焦点和 App 兜底两条键盘路径上尝试永久授权
async def test_external_options_and_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    target = "C:/outside/" + "very-long-directory/" * 8 + "[secret].txt"
    client = Client({
        "type": "permission.requested", "session_id": "sess-one", "tool_use_id": "tool-one",
        "tool_name": "read_file", "params": {"path": "../[secret].txt"},
        "param_preview": "short", "workspace": "C:/repo", "resolved_path": target,
        "outside_workspace": True, "allowed_decisions": ["allow_once", "deny_once"],
    })
    async with app.run_test(size=(100, 45)) as pilot:
        app._client = client
        await app._open_session("sess-one")
        await pilot.pause()
        select = app.query_one(PermissionSelect)
        assert [choice[0] for choice in select._choices] == ["allow_once", "deny_once"]
        assert len(select.query(Button)) == 2
        assert not select.query("#permission-always_allow")
        details = str(app.query_one(PermissionBlock).render())
        assert target in details
        assert "../[secret].txt" in details
        assert "C:/repo" in details
        await pilot.press("a", "2", "d", "4")
        select._pick("always_allow")
        # 失焦时 App 的兜底同样只能读取当前有效快捷键
        for key in ("a", "2", "d", "4"):
            app.set_focus(None)
            app.on_key(events.Key(key, key))
        await pilot.pause()
        assert len(client.calls) == 1
        select.focus()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert client.calls[-1] == (
            "permission.respond", {"request_id": "approval-one", "decision": "deny_once",
                                   "session_id": "sess-one", "run_id": "run-one"},
        )


# 功能：验证外部审批按钮允许一次，普通审批仍有四个选项
# 设计：使用真实按钮点击确认消息路由，而非只检查按钮标签
async def test_approval_buttons_and_ordinary_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client({
        "type": "permission.requested", "tool_use_id": "tool-one", "tool_name": "list_dir",
        "params": {"path": ".."}, "outside_workspace": True,
        "allowed_decisions": ["allow_once", "deny_once"],
    })
    async with app.run_test(size=(100, 45)) as pilot:
        app._client = client
        await app._open_session("sess-one")
        await pilot.pause()
        await pilot.click("#permission-allow_once")
        await pilot.pause()
        assert client.calls[-1][1]["decision"] == "allow_once"
        app._handle_event_inner({
            "type": "permission.requested", "tool_use_id": "ordinary", "tool_name": "bash",
            "request_id": "approval-ordinary", "session_id": "sess-one", "run_id": "run-two",
            "params": {"command": "echo hello"}, "param_preview": "echo hello",
        })
        await pilot.pause()
        select = app.query_one(PermissionSelect)
        assert len(select._choices) == 4
        assert len(select.query(Button)) == 4
        await pilot.press("a")
        await pilot.pause()
        assert client.calls[-1][1] == {
            "request_id": "approval-ordinary", "session_id": "sess-one", "run_id": "run-two",
            "decision": "always_allow",
        }
