from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import _daemon_diagnostics, _wait_for_daemon


async def test_startup_exited_process_reports_status_and_stderr(tmp_path, monkeypatch):
    stderr = tmp_path / "stderr.log"
    stderr.write_text("controlled startup failure", encoding="utf-8")
    process = SimpleNamespace(pid=123, poll=lambda: 7)
    connect = AsyncMock()
    monkeypatch.setattr("tests.conftest.asyncio.open_connection", connect)
    with pytest.raises(pytest.fail.Exception) as failure:
        await _wait_for_daemon(process, 7437, stderr)
    assert "exit_code=7" in str(failure.value)
    assert "controlled startup failure" in str(failure.value)
    connect.assert_not_called()


async def test_startup_deadline_reports_still_running_process(tmp_path):
    stderr = tmp_path / "stderr.log"
    stderr.write_text("loading dependencies", encoding="utf-8")
    process = SimpleNamespace(pid=456, poll=lambda: None)
    with pytest.raises(pytest.fail.Exception) as failure:
        await _wait_for_daemon(process, 7437, stderr, timeout_s=0)
    assert "did not start within 0 seconds" in str(failure.value)
    assert "pid=456, exit_code=None" in str(failure.value)
    assert "loading dependencies" in str(failure.value)


def test_startup_diagnostics_only_reads_stderr_tail(tmp_path):
    stderr = tmp_path / "stderr.log"
    stderr.write_bytes(b"earlier detail" + b"x" * 8192 + b"last detail")
    diagnostics = _daemon_diagnostics(SimpleNamespace(pid=1, poll=lambda: 2), stderr)
    assert "earlier detail" not in diagnostics and diagnostics.endswith("last detail")
    assert len(diagnostics) < 8300
