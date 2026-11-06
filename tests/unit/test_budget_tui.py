from __future__ import annotations

from textual.widgets import Static

from kama_claude.tui.app import KamaTuiApp
from tests.unit.test_tui_recovery import Client, idle_socket


# 功能：验证预算状态只更新当前会话的同一条显示，子 Run 不混入主任务
# 设计：运行真实 Textual 生命周期，两次更新同一个 Run 后检查控件数量与内容
async def test_budget_status_updates_one_widget(monkeypatch):
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 36)) as pilot:
        app._client = Client(status="active")
        await app._open_session("sess-test")
        event = {
            "type": "run.budget", "run_id": "run-test", "method": "usage_anchor",
            "estimated_input_tokens": 100, "actual_tokens": 0, "estimated_tokens": 110,
            "elapsed_seconds": 1, "trigger_tokens": 800,
        }
        app._handle_event_inner(event)
        app._handle_event_inner({**event, "actual_tokens": 107, "estimated_tokens": 0})
        await pilot.pause()
        assert len(app.query("#budget-run-test")) == 1
        text = str(app.query_one("#budget-run-test", Static).render())
        assert "107" in text and "Usage+增量" in text and "压缩阈值 800" in text
        app._view.subagent_runs["child"] = "run-test"
        app._handle_event_inner({**event, "run_id": "child"})
        assert not list(app.query("#budget-child"))
        app._handle_event_inner({
            **event, "method": "subagent_activity", "actual_tokens": 307,
            "estimated_tokens": 0, "child_actual_tokens": 200,
        })
        text = str(app.query_one("#budget-run-test", Static).render())
        assert "任务树用量更新" in text and "累计已知 307" in text and "后代 200" in text
