from __future__ import annotations

import pytest
from textual.widgets import Static

from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.widgets.usage import ReplyUsage
from tests.unit.test_tui_recovery import idle_socket


def text(widget):
    return str(widget.render())


def token(value="abcd", **extra):
    return {"type": "llm.token", "run_id": "root", "token": value,
            "input_tokens_estimate": 100, "context_window_tokens": 1000, **extra}


def usage(**extra):
    return {"type": "llm.usage", "run_id": "root", "input_tokens": 20,
            "total_input_tokens": 100, "output_tokens": 25, "cache_read_input_tokens": 80,
            "context_window_tokens": 1000, **extra}


def test_stream_estimate_is_replaced_by_real_reply_usage():
    widget = ReplyUsage()
    widget.append_output(token("你"))
    widget.append_output(token("好"))
    assert widget.output_tokens == 2 and widget.context_pct == pytest.approx(0.102)
    assert "输入 ≈100" in text(widget) and "输出 ≈2" in text(widget)
    assert "缓存命中 —" in text(widget)
    widget.finish(usage())
    assert widget.output_tokens == 25 and widget.context_pct == pytest.approx(0.125)
    assert "输入 100 · 输出 25 · 缓存命中 80.0% · 上下文 12.5%" in text(widget)
    assert "≈" not in text(widget)


@pytest.mark.parametrize(("total", "cache", "created", "pct"), [
    (110, 80, 10, "72.7%"), (100, 80, 0, "80.0%"),
])
def test_cache_rate_uses_total_input_without_double_counting(total, cache, created, pct):
    widget = ReplyUsage()
    widget.finish(usage(total_input_tokens=total, cache_read_input_tokens=cache,
                        cache_creation_input_tokens=created))
    assert f"缓存命中 {pct}" in text(widget)
    assert widget.context_pct == (total + 25) / 1000


def test_stream_fragment_boundaries_do_not_change_output_estimate():
    whole, split = ReplyUsage(), ReplyUsage()
    whole.append_output(token("abcd中文"))
    for part in "abcd中文":
        split.append_output(token(part))
    assert split.output_tokens == whole.output_tokens == 3


def test_missing_or_zero_usage_does_not_show_invented_rate():
    widget = ReplyUsage()
    widget.append_output({"token": "hello"})
    assert "输入 —" in text(widget) and "上下文 —" in text(widget)
    widget.finish(usage(input_tokens=0, total_input_tokens=0, output_tokens=0,
                        cache_read_input_tokens=0))
    assert "缓存命中 —" in text(widget) and "上下文 0.0%" in text(widget)


def test_old_events_can_infer_window_from_input_ratio():
    widget = ReplyUsage()
    event = usage(total_input_tokens=200, output_tokens=20, context_pct=0.1)
    event.pop("context_window_tokens")
    widget.finish(event)
    assert widget.window_tokens == 2000 and "上下文 11.0%" in text(widget)


async def test_each_step_owns_one_usage_row_and_child_does_not_overwrite_it(monkeypatch):
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 36)) as pilot:
        app._handle_event_inner({"type": "step.started", "run_id": "root", "step": 1})
        app._handle_event_inner(token())
        app._handle_event_inner(token())
        app._handle_event_inner(usage())
        await pilot.pause()
        rows = list(app.query(ReplyUsage))
        assert len(rows) == 1 and "输入 100" in text(rows[0])
        first = text(rows[0])
        app._handle_event_inner({"type": "subagent.started", "run_id": "child", "description": "read"})
        app._handle_event_inner(token("child text", run_id="child"))
        app._handle_event_inner(usage(run_id="child", total_input_tokens=999))
        assert text(rows[0]) == first
        app._handle_event_inner({"type": "step.started", "run_id": "root", "step": 2})
        app._handle_event_inner(usage(total_input_tokens=200))  # 无文本的纯工具回复也显示用量。
        await pilot.pause()
        rows = list(app.query(ReplyUsage))
        assert len(rows) == 2 and text(rows[0]) == first
        assert "输入 200" in text(rows[1]) and "上下文 22.5%" in text(rows[1])
        assert app._view.last_context_pct == 0.225
        all_text = "\n".join(text(widget) for widget in app.query(Static))
        assert "累计已知" not in all_text and "压缩阈值" not in all_text


async def test_model_retry_starts_fresh_stream_estimate(monkeypatch):
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(110, 36)) as pilot:
        app._handle_event_inner(token("a" * 400))
        app._handle_event_inner({"type": "run.budget", "run_id": "root", "reason": "model_retry"})
        app._handle_event_inner(token("abcd"))
        await pilot.pause()
        rows = list(app.query(ReplyUsage))
        assert len(rows) == 2 and rows[0].output_tokens == 100 and rows[1].output_tokens == 1
