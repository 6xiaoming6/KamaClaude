from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from textual.widgets import Button

from kama_claude.tui.app import ChatTextArea, KamaTuiApp, ToolCallBlock
from kama_claude.tui.recovery import RecoveryActions, SessionPicker


# 构造真实快照形状的假后端，不调用模型
class Client:
    # 保存当前会话状态和收到的命令
    def __init__(self, status: str = "interrupted", unknown: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.state = {
            "session": {"id": "sess-test", "status": status, "title": "test", "mode": "chat"},
            "revision": 7,
            "messages": [{"role": "user", "content": "goal"}],
            "running": False,
            "pending_permissions": [],
            "event_seq": 0,
            "run": {
                "run_id": "run-test",
                "status": status,
                "step": 1,
                "reason": "core_restarted",
                "tools": [
                    {
                        "id": "tool-a",
                        "name": "write_file",
                        "input": {"path": "a.txt"},
                        "status": "unknown" if unknown else "pending",
                    }
                ],
                "subagents": {},
            }
            if status == "interrupted"
            else None,
        }

    # 返回权威状态，并在用户确认后模拟任务被登记
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "session.open":
            return json.loads(json.dumps(self.state))
        if method == "session.list":
            return {"sessions": [self.state["session"]]}
        if method == "session.resume":
            aborted = params["action"] == "abort"
            self.state["revision"] += 1
            self.state["session"]["status"] = "waiting_for_input" if aborted else "active"
            self.state["running"] = not aborted
            self.state["run"]["status"] = "failed" if aborted else "running"
            return {"run_id": "run-test", "status": self.state["run"]["status"], "revision": 8}
        raise AssertionError(method)


# 保留 Textual 生命周期，但不建立测试之外的网络连接
async def idle_socket(self: KamaTuiApp) -> None:
    await asyncio.Future()


# 功能：验证未知工具快照显示明确确认入口，点击前不会自动续跑
# 设计：在真实 Textual 测试屏幕上打开快照并点击按钮，检查实际 RPC 参数与修订号
async def test_unknown_tool_requires_button_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client(unknown=True)
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        await app._open_session("sess-test")
        await pilot.pause()
        assert app.query_one(RecoveryActions)
        assert app.query_one(ChatTextArea).disabled
        assert not any(method == "session.resume" for method, _ in client.calls)
        await pilot.click("#recovery-retry")
        await pilot.pause()
        resumes = [params for method, params in client.calls if method == "session.resume"]
        assert resumes == [
            {
                "session_id": "sess-test",
                "expected_revision": 7,
                "action": "retry_tool",
            }
        ]
        assert app._busy


# 功能：验证普通中断提供继续和终止按钮，并能在终止后恢复输入
# 设计：点击终止按钮后重新读取后端快照，避免只在本地伪造可输入状态
async def test_abort_refreshes_idle_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client()
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        await app._open_session("sess-test")
        await pilot.pause()
        assert app.query_one("#recovery-continue", Button)
        await pilot.click("#recovery-abort")
        await pilot.pause()
        assert app._busy is False
        assert not app.query_one(ChatTextArea).disabled
        assert not list(app.query(RecoveryActions))


# 功能：验证其他会话的关闭或等待输入事件不改变当前界面
# 设计：使用真实组件检查输入状态，而不是只断言某个过滤函数返回值
async def test_foreign_session_events_do_not_close_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client(status="waiting_for_input")
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        await app._open_session("sess-test")
        app._busy = True
        app._handle_event({"type": "session.waiting_for_input", "session_id": "sess-other"})
        assert app._busy is True
        app._handle_event({"type": "session.closed", "session_id": "sess-other"})
        assert not app.query_one(ChatTextArea).disabled
        assert app._client is client
        await pilot.pause()


# 功能：验证历史会话选择不会隐式创建新会话或续跑任务
# 设计：通过实际 ModalScreen 和 ListView 选择历史，检查用户路径与 RPC 调用
async def test_history_picker_opens_existing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client(status="waiting_for_input")
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        worker = app.run_worker(app._pick_session(), name="test_picker")
        await pilot.pause()
        assert isinstance(app.screen, SessionPicker)
        await pilot.press("enter")
        await worker.wait()
        assert app._session_id == "sess-test"
        assert [method for method, _ in client.calls] == ["session.list", "session.open"]


# 功能：验证快照恢复的工具结果不会被迟到成功事件重复修改
# 设计：先渲染已完成工具，再注入同一工具的通知，检查组件身份与状态
async def test_snapshot_tool_result_is_not_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client()
    client.state["run"]["tools"][0]["status"] = "completed"
    client.state["messages"] = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-a", "name": "write_file", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-a", "content": "saved"},
            ],
        },
    ]
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        await app._open_session("sess-test")
        await pilot.pause()
        blocks = list(app.query(ToolCallBlock))
        assert len(blocks) == 1 and blocks[0]._output == "saved"
        app._handle_event(
            {
                "type": "tool.call_finished",
                "session_id": "sess-test",
                "run_id": "run-test",
                "tool_use_id": "tool-a",
                "output": "duplicate",
                "event_seq": 1,
            }
        )
        assert len(list(app.query(ToolCallBlock))) == 1
        assert blocks[0]._output == "saved"


# 功能：验证退出界面不主动关闭会话
# 设计：替换 exit 以观察调用，假后端拒绝任何未预期的关闭 RPC
async def test_quit_does_not_close_session(monkeypatch: pytest.MonkeyPatch) -> None:
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client()
    app._client = client
    app._session_id = "sess-test"
    exits: list[bool] = []
    monkeypatch.setattr(app, "exit", lambda: exits.append(True))
    await app.action_quit()
    assert exits == [True] and client.calls == []


# 功能：验证打开快照期间的旧事件被丢弃，新事件仅渲染一次
# 设计：模拟 RPC 返回前已入缓冲的关闭事件，再对同序号 token 重复投递
async def test_open_snapshot_buffers_and_deduplicates_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client(status="waiting_for_input")
    original = client.send_command

    # 在同步窗口插入比快照水位旧的通知
    async def open_with_push(method: str, params: dict[str, Any]) -> dict[str, Any]:
        state = await original(method, params)
        if method == "session.open":
            app._event_buffer.append(
                {
                    "type": "session.closed",
                    "session_id": "sess-test",
                    "event_seq": 11,
                }
            )
            state["event_seq"] = 12
        return state

    client.send_command = open_with_push
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = client
        await app._open_session("sess-test")
        assert not app.query_one(ChatTextArea).disabled
        token = {
            "type": "llm.token",
            "session_id": "sess-test",
            "run_id": "run-test",
            "event_seq": 13,
            "token": "once",
        }
        app._handle_event(token)
        app._handle_event(token)
        assert app._current_llm._text == "once"
        await pilot.pause()
