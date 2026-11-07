from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from kama_claude.cli.commands import chat, core, run, trace
from kama_claude.cli.permissions import PermissionPrompts
from kama_claude.core.app import CoreApp
from kama_claude.core.bus.commands import PongResult
from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.config import KamaConfig
from kama_claude.core.trace.record import TraceRecord
from tests.unit.test_permission_tui import approval


@pytest.mark.parametrize("child", [False, True])
async def test_run_handles_approval_while_request_waits(monkeypatch, capsys, child) -> None:
    approved = asyncio.Event()
    calls = []

    class Client:
        async def connect(self):
            pass

        async def close(self):
            pass

        def on_event(self, handler):
            self.handler = handler

        async def run_event_loop(self):
            await asyncio.Future()

        async def send_command(self, method, params):
            calls.append((method, params))
            if method == "session.create":
                return {"session_id": "sess-one"}
            if method == "permission.respond":
                approved.set()
            if method == "session.send_message":
                await self.handler({"type": "run.started", "run_id": "root"})
                event = approval()
                event["run_id"] = "child-a" if child else "root"
                await self.handler(event)
                await asyncio.wait_for(approved.wait(), 1)
                await self.handler(
                    {"type": "run.finished", "run_id": "child-a", "status": "failed"}
                )
                await self.handler(
                    {
                        "type": "run.finished",
                        "run_id": "root",
                        "status": "success",
                        "result": "done",
                        "steps": 1,
                    }
                )
                return {"run_id": "root"}
            return {}

    inputs = iter(["a", "y"])

    async def readline(prompt):
        return next(inputs)

    monkeypatch.setattr(run, "SocketClient", lambda *args: Client())
    monkeypatch.setattr(run, "_readline", readline)
    assert await asyncio.wait_for(run._run_async("goal", KamaConfig()), 2) == 0
    response = next(p for m, p in calls if m == "permission.respond")
    assert response["decision"] == "allow_once"
    assert response["run_id"] == ("child-a" if child else "root")
    assert next(p for m, p in calls if m == "event.subscribe")["scope"] == "session:sess-one"
    assert "choose one" in capsys.readouterr().out


async def test_run_eof_denies_approval(monkeypatch) -> None:
    answered = asyncio.Event()

    class Client:
        async def connect(self):
            pass

        async def close(self):
            pass

        def on_event(self, handler):
            self.handler = handler

        async def run_event_loop(self):
            await asyncio.Future()

        async def send_command(self, method, params):
            if method == "session.create":
                return {"session_id": "sess-one"}
            if method == "permission.respond":
                assert params["decision"] == "deny_once"
                answered.set()
            if method == "session.send_message":
                await self.handler({"type": "run.started", "run_id": "root"})
                await self.handler(approval())
                await answered.wait()
                await self.handler({"type": "run.finished", "run_id": "root", "status": "success"})
            return {}

    async def eof(prompt):
        raise EOFError

    monkeypatch.setattr(run, "SocketClient", lambda *args: Client())
    monkeypatch.setattr(run, "_readline", eof)
    assert await asyncio.wait_for(run._run_async("goal", KamaConfig()), 2) == 1


@pytest.mark.parametrize("interrupted", [False, True])
async def test_chat_stops_waiting_input_on_disconnect_or_interrupt(
    monkeypatch, capsys, interrupted
):
    subscribed = asyncio.Event()
    calls = []

    class Client:
        async def connect(self):
            pass

        async def close(self):
            pass

        def on_event(self, handler):
            self.handler = handler

        async def run_event_loop(self):
            await subscribed.wait()
            if interrupted:
                await self.handler(
                    {"type": "run.interrupted", "run_id": "root", "reason": "unknown"}
                )
                await asyncio.Future()

        async def send_command(self, method, params):
            calls.append(method)
            if method == "session.create":
                return {"session_id": "s"}
            if method == "event.subscribe":
                subscribed.set()
            return {}

    async def blocked(prompt):
        await asyncio.Future()

    monkeypatch.setattr(chat, "SocketClient", lambda *args: Client())
    monkeypatch.setattr(chat, "_readline", blocked)
    assert await asyncio.wait_for(chat._chat_async(KamaConfig()), 2) == 1
    assert "session.close" not in calls
    output = capsys.readouterr().err
    assert ("interrupted" if interrupted else "disconnected") in output


async def test_child_interruption_does_not_end_parent_chat() -> None:
    printer = chat.ChatPrinter()
    await printer.handle({"type": "run.started", "run_id": "root"})
    await printer.handle({"type": "run.started", "run_id": "child"})
    await printer.handle({"type": "run.interrupted", "run_id": "child", "reason": "unknown"})
    assert not printer.interrupted.is_set()


async def test_expired_approval_answer_cannot_approve_next_request() -> None:
    prompts = PermissionPrompts()
    prompts.handle(approval("one"))
    prompts.handle(approval("two"))
    prompts.resolve("one")
    client = Mock(send_command=AsyncMock())
    await prompts.answer(client, "one", "y")
    client.send_command.assert_not_awaited()
    assert list(prompts.pending) == ["two"]


def test_blocked_console_input_does_not_block_process_exit() -> None:
    # 不关闭 stdin 来制造 EOF：主协程超时退出后，进程必须仍能自行结束。
    script = """
import asyncio
from kama_claude.cli.console import readline
async def main():
    try: await asyncio.wait_for(readline(''), .05)
    except TimeoutError: pass
asyncio.run(main())
print('exited')
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.wait(timeout=5) == 0
        assert b"exited" in proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


async def test_port_open_is_not_enough_for_core_status() -> None:
    async def other_service(reader, writer):
        await reader.readline()
        writer.write(b'{"jsonrpc":"2.0","id":"other","result":{}}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(other_service, "127.0.0.1", 0)
    async with server:
        with pytest.raises((OSError, TimeoutError)):
            await core._ping_check(KamaConfig(port=server.sockets[0].getsockname()[1]))


async def test_startup_detects_exited_child_before_ping() -> None:
    child = Mock(pid=123, returncode=2, poll=Mock(return_value=2))
    with pytest.raises(RuntimeError, match="exit_code=2"):
        await core._wait_ready(child, KamaConfig())


async def test_startup_rejects_other_core_pid(monkeypatch) -> None:
    child = Mock(pid=123, poll=Mock(return_value=None))
    pong = PongResult(server_version="test", uptime_ms=1, received_at="now", pid=456)
    monkeypatch.setattr(core, "_ping_check", AsyncMock(return_value=pong))
    monkeypatch.setattr(core.psutil, "Process", lambda pid: Mock(children=Mock(return_value=[])))
    with pytest.raises(RuntimeError, match="another process"):
        await core._wait_ready(child, KamaConfig())


@pytest.mark.parametrize("kind", ["time", "module", "exe"])
def test_reused_pid_is_never_stopped(monkeypatch, kind) -> None:
    process = Mock(
        is_running=Mock(return_value=True),
        create_time=Mock(return_value=1.0),
        exe=Mock(return_value=sys.executable),
        cmdline=Mock(return_value=["python", "-m", "kama_claude.core"]),
    )
    if kind == "time":
        process.create_time.return_value = 2.0
    if kind == "module":
        process.cmdline.return_value = ["python", "unrelated.py"]
    if kind == "exe":
        process.exe.return_value = "different.exe"
    monkeypatch.setattr(core.psutil, "Process", lambda pid: process)
    info = core.DaemonInfo(
        pid=123, created_at=1.0, executable=sys.executable, host="127.0.0.1", port=7437
    )
    with pytest.raises(RuntimeError, match="identity"):
        info.process()
    process.terminate.assert_not_called()


def test_legacy_pid_record_is_not_trusted(tmp_path, monkeypatch) -> None:
    path = tmp_path / "core.pid"
    path.write_text("123")
    monkeypatch.setattr(core, "_PID_FILE", path)
    with pytest.raises(RuntimeError, match="legacy"):
        core._load_info()
    assert path.exists()


async def test_shutdown_rejects_wrong_process_identity() -> None:
    app = CoreApp()
    with pytest.raises(HandlerError):
        await app._shutdown_handler({"expected_pid": os.getpid() + 1, "expected_created_at": 1.0})
    assert not app._shutdown.is_set()


def test_trace_reads_utf8_and_follows_same_handle(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "trace.jsonl"
    record = TraceRecord(
        ts="now", direction="CORE", layer="event", kind="event", data={"text": "中文"}
    )
    path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    config = KamaConfig()
    config.trace.file = str(path)
    sleeps = 0

    def wait(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            with path.open("a", encoding="utf-8") as f:
                f.write(record.model_dump_json() + "\n")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(trace.time, "sleep", wait)
    with pytest.raises(KeyboardInterrupt):
        trace.cmd_trace(None, config, raw=True, follow=True)
    assert capsys.readouterr().out.count("中文") == 2


def test_failed_start_never_reports_started_or_saves_pid(tmp_path, monkeypatch, capsys):
    pid_file = tmp_path / "core.pid"
    monkeypatch.setattr(core, "_PID_FILE", pid_file)
    monkeypatch.setattr(core, "_ping_check", AsyncMock(side_effect=ConnectionRefusedError()))
    child = Mock(pid=123, returncode=2, poll=Mock(return_value=2))
    monkeypatch.setattr(core.subprocess, "Popen", Mock(return_value=child))
    with pytest.raises(SystemExit) as error:
        core.cmd_core_start(KamaConfig())
    assert error.value.code == 1 and not pid_file.exists()
    output = capsys.readouterr()
    assert "started" not in output.out and "exit_code=2" in output.err
    assert "core-startup.log" in output.err


def test_stop_timeout_retains_identity_without_force_kill(tmp_path, monkeypatch):
    import psutil

    pid_file = tmp_path / "core.pid"
    info = core.DaemonInfo(
        pid=123, created_at=1.0, executable=sys.executable, host="127.0.0.1", port=7437
    )
    pid_file.write_text(info.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(core, "_PID_FILE", pid_file)
    process = Mock(
        is_running=Mock(return_value=True),
        create_time=Mock(return_value=1.0),
        exe=Mock(return_value=sys.executable),
        cmdline=Mock(return_value=["python", "-m", "kama_claude.core"]),
    )
    process.wait.side_effect = psutil.TimeoutExpired(1)
    monkeypatch.setattr(core.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(core, "request", AsyncMock(return_value={"accepted": True}))
    with pytest.raises(SystemExit):
        core.cmd_core_stop(KamaConfig())
    assert pid_file.exists()
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_console_pipe_keeps_unicode_and_following_line():
    code = """
import asyncio,json
from kama_claude.cli.console import readline
async def main():
    return [await readline(''), await readline('')]
print(json.dumps(asyncio.run(main()),ensure_ascii=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        input="中文\nnext\n".encode(),
        capture_output=True,
        timeout=5,
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert completed.returncode == 0
    assert b"\\u4e2d\\u6587" in completed.stdout and b"next" in completed.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows console API adapter")
def test_windows_console_adapter_reads_unicode(monkeypatch):
    import ctypes
    import msvcrt

    from kama_claude.cli.console import _read_windows_console

    def read(handle, buffer, size, count, reserved):
        buffer.value = "中文\r\n"
        count._obj.value = 4
        return 1

    kernel = Mock(ReadConsoleW=Mock(side_effect=read))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel)
    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda fd: 2**40)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    assert _read_windows_console() == "中文"
