from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from kama_claude.core.trace.writer import TraceWriter
from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.widgets.conversation import ToolCallBlock
from tests.unit.test_trace_writer import _record
from tests.unit.test_workspace_tui import idle_socket


# 两个子任务可收到同样的模型工具 ID，结果必须落在各自的控件上。
async def test_tool_ids_are_scoped_by_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    async with app.run_test(size=(100, 40)) as pilot:
        for run_id in ("a", "b"):
            app._handle_event_inner({
                "type": "tool.call_started", "run_id": run_id,
                "tool_use_id": "same", "tool_name": "read_file", "params": {"path": run_id},
            })
        await pilot.pause()
        blocks = list(app.query(ToolCallBlock))
        for run_id in ("b", "a"):
            app._handle_event_inner({
                "type": "tool.call_finished", "run_id": run_id, "tool_use_id": "same",
                "elapsed_ms": 1, "output": f"result-{run_id}",
            })
        await pilot.pause()
        assert [block._output for block in blocks] == ["result-a", "result-b"]


# 先确定后台任务已经失败，再检查 stop，而不是用文件系统权限碰运气。
async def test_trace_open_failure_does_not_hang_stop(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path / "trace.jsonl")
    with patch("builtins.open", side_effect=OSError("simulated full disk")):
        await writer.start()
        writer.emit(_record())
        await asyncio.sleep(0)
        assert writer._task is not None and writer._task.done()
        try:
            await asyncio.wait_for(writer.stop(), 0.2)
        finally:
            # 失败基线也回收异常，避免复现测试污染事件循环。
            await asyncio.gather(writer._task, return_exceptions=True)
    assert writer._queue.empty()


async def test_trace_write_failure_discards_remaining_records(tmp_path: Path) -> None:
    from unittest.mock import mock_open

    stream = mock_open()
    stream.return_value.write.side_effect = OSError("simulated write failure")
    writer = TraceWriter(tmp_path / "trace.jsonl")
    with patch("builtins.open", stream):
        await writer.start()
        for _ in range(3):
            writer.emit(_record())
        await asyncio.sleep(0)
        assert writer._failure is not None
        writer.emit(_record())
        await asyncio.wait_for(writer.stop(), 0.2)
        await writer.stop()
    assert writer._dropped == 4 and writer._closed
    await asyncio.wait_for(writer._queue.join(), 0.2)


async def test_trace_shutdown_has_bounded_wait(tmp_path: Path, monkeypatch) -> None:
    writer = TraceWriter(tmp_path / "trace.jsonl")
    async def stalled() -> None:
        await asyncio.sleep(30)
    monkeypatch.setattr(writer, "_drain", stalled)
    await writer.start()
    writer.emit(_record())
    await asyncio.wait_for(writer.stop(timeout_s=0.01), 0.3)
    assert writer._task.done() and writer._queue.empty() and writer._dropped == 1


@pytest.mark.parametrize("metadata", [True, False])
async def test_history_tool_results_are_paired_without_crossing_runs(monkeypatch, metadata) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    messages = []
    for run_id in ("a", "b"):
        identity = {"run_id": run_id} if metadata else {}
        messages.extend([
            {**identity, "role": "assistant", "content": [
                {"type": "tool_use", "id": "same", "name": "read_file", "input": {}},
            ]},
            {**identity, "role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "same", "content": f"result-{run_id}"},
            ]},
        ])
    async with app.run_test(size=(100, 40)) as pilot:
        app._view.render_history(messages)
        await pilot.pause()
        if metadata:
            app._handle_event_inner({
                "type": "tool.call_finished", "run_id": "a", "tool_use_id": "same",
                "output": "duplicate result must be ignored", "elapsed_ms": 1,
            })
        assert [block._output for block in app.query(ToolCallBlock)] == ["result-a", "result-b"]
        assert not app._view.pending_tools
