from __future__ import annotations

from textual.widgets import Static

from kama_claude.tui.app import KamaTuiApp
from tests.unit.test_tui_recovery import Client, idle_socket


# 预算仍由后端记账；普通预算更新不再额外渲染成用量行。
async def test_budget_updates_do_not_add_usage_widgets(monkeypatch):
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
        assert not app.query(".usage")
        app._view.subagent_runs["child"] = "run-test"
        app._handle_event_inner({**event, "run_id": "child"})
        assert not list(app.query("#budget-child"))
        app._handle_event_inner({
            **event, "method": "subagent_activity", "actual_tokens": 307,
            "estimated_tokens": 0, "child_actual_tokens": 200,
        })
        await pilot.pause()
        assert not app.query(".usage")
        text = "\n".join(str(widget.render()) for widget in app.query(Static))
        assert "累计已知" not in text and "估算占用" not in text


async def test_retry_and_terminal_reason_remain_visible(monkeypatch):
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 36)) as pilot:
        app._handle_event_inner({"type": "run.budget", "run_id": "root", "reason": "model_retry"})
        app._handle_event_inner({"type": "run.finished", "run_id": "root", "status": "failed",
                                 "reason": "run_token_budget_exceeded", "steps": 2})
        await pilot.pause()
        text = "\n".join(str(widget.render()) for widget in app.query(Static))
        assert "重新请求" in text and "run_token_budget_exceeded" in text
        assert "累计已知" not in text and "估算占用" not in text
