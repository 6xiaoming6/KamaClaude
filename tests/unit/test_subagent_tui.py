from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static

from kama_claude.tui.app import KamaTuiApp


# 保留真实界面生命周期，不建立测试外的 TCP 连接
async def idle_socket(self: KamaTuiApp) -> None:
    await asyncio.Future()


# 子任务保留终态与原因，不再混入主对话的累计用量显示。
async def test_subagent_state_without_cumulative_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 35)) as pilot:
        app._handle_event_inner({
            "type": "subagent.started", "run_id": "child", "description": "review code",
        })
        app._handle_event_inner({
            "type": "run.budget", "run_id": "child", "actual_tokens": 115,
        })
        assert not app.query("#budget-child")
        app._handle_event_inner({
            "type": "subagent.finished", "run_id": "child", "status": "failed",
            "reason": "run_token_budget_exceeded", "actual_tokens": 115, "estimated_tokens": 50,
        })
        await pilot.pause()
        content = "\n".join(str(widget.render()) for widget in app.query(Static))
        assert "已知 115" not in content and "估算 50" not in content
        assert "run_token_budget_exceeded" in content
