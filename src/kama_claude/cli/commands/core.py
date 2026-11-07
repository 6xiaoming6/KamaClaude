from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kama_claude.cli.rpc import ping, request
from kama_claude.core.bus.commands import PongResult
from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError

_PID_FILE = Path.home() / ".kama" / "kama-core.pid"
_START_TIMEOUT = 15.0
_STOP_TIMEOUT = 15.0


class DaemonInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pid: int = Field(gt=0)
    created_at: float = Field(gt=0)
    executable: str
    host: str
    port: int = Field(ge=1, le=65535)

    def process(self) -> psutil.Process | None:
        try:
            process = psutil.Process(self.pid)
            if not process.is_running():
                return None
            # PID、创建时间、解释器和模块一起核对；禁止用 os.kill(pid, 0) 探活。
            if (abs(process.create_time() - self.created_at) > 0.001
                    or os.path.normcase(process.exe()) != os.path.normcase(self.executable)
                    or process.cmdline()[1:] != ["-m", "kama_claude.core"]):
                raise RuntimeError("PID identity changed; refusing to stop this process")
            return process
        except psutil.NoSuchProcess:
            return None


async def _ping_check(config: KamaConfig) -> PongResult:
    try:
        return await ping(config)
    except (IpcError, ValueError) as exc:
        raise ConnectionError("endpoint did not return a valid KamaClaude pong") from exc


def _load_info() -> DaemonInfo | None:
    if not _PID_FILE.exists():
        return None
    try:
        return DaemonInfo.model_validate_json(_PID_FILE.read_text(encoding="utf-8"))
    except (ValueError, ValidationError) as exc:
        raise RuntimeError(f"unverified/legacy PID record: {_PID_FILE}; "
                           "no process was stopped") from exc


def _remove_record(info: DaemonInfo) -> None:
    if _load_info() == info:
        _PID_FILE.unlink(missing_ok=True)


def cmd_core_status(config: KamaConfig) -> None:
    try:
        pong = asyncio.run(_ping_check(config))
        print(f"running  pid={pong.pid}  ({config.host}:{config.port})")
    except (OSError, TimeoutError):
        print("not running (no valid KamaClaude response)")
        raise SystemExit(1)


async def _wait_ready(proc: subprocess.Popen[bytes], config: KamaConfig) -> PongResult:
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Core exited before ready: exit_code={proc.returncode}")
        try:
            pong = await asyncio.wait_for(_ping_check(config), 0.5)
            descendants = {child.pid for child in psutil.Process(proc.pid).children(recursive=True)}
            if pong.pid != proc.pid and pong.pid not in descendants:
                raise RuntimeError("port belongs to another process; startup was not confirmed")
            if proc.poll() is None:
                return pong
        except (OSError, TimeoutError):
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Core did not become ready within {_START_TIMEOUT:g}s")


def _cleanup_startup(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        root = psutil.Process(proc.pid)
        processes = [*reversed(root.children(recursive=True)), root]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(processes, timeout=2)
    for process in remaining:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(remaining, timeout=2)
    if remaining:
        print("error: startup process cleanup incomplete", file=sys.stderr)
    proc.wait(timeout=2)


def cmd_core_start(config: KamaConfig) -> None:
    try:
        pong = asyncio.run(_ping_check(config))
        print(f"already running  pid={pong.pid}  ({config.host}:{config.port})")
        return
    except (OSError, TimeoutError):
        pass
    proc: subprocess.Popen[bytes] | None = None
    log_path = _PID_FILE.parent / "logs" / "core-startup.log"
    try:
        old = _load_info()
        if old is not None and old.process() is not None:
            raise RuntimeError("managed Core still exists but is not ready; inspect its log first")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, KAMA_HOST=config.host, KAMA_PORT=str(config.port), PYTHONUTF8="1")
        with log_path.open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "kama_claude.core"], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        pong = asyncio.run(_wait_ready(proc, config))
        process = psutil.Process(pong.pid)
        info = DaemonInfo(pid=pong.pid, created_at=process.create_time(), executable=process.exe(),
                          host=config.host, port=config.port)
        temporary = _PID_FILE.with_suffix(".tmp")
        try:
            temporary.write_text(info.model_dump_json(), encoding="utf-8")
            temporary.replace(_PID_FILE)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"started  pid={info.pid}  ({config.host}:{config.port}); log={log_path}")
    except (OSError, RuntimeError, psutil.Error, TimeoutError, KeyboardInterrupt) as exc:
        if proc is not None:
            # 只回收本次创建的子进程；正常 core stop 不走强制终止。
            try:
                _cleanup_startup(proc)
            except (OSError, psutil.Error, subprocess.TimeoutExpired) as cleanup:
                print(f"error: startup cleanup failed: {cleanup}", file=sys.stderr)
        print(f"error: {exc}; startup log={log_path}", file=sys.stderr)
        raise SystemExit(130 if isinstance(exc, KeyboardInterrupt) else 1) from exc


def cmd_core_stop(config: KamaConfig) -> None:
    try:
        info = _load_info()
        if info is None:
            print("not managed: no verified PID record; stop a foreground Core with Ctrl+C")
            return
        if (info.host, info.port) != (config.host, config.port):
            raise RuntimeError("PID record belongs to another endpoint; refusing shutdown")
        process = info.process()
        if process is None:
            _remove_record(info)
            print("not running (removed stale PID record)")
            return
        try:
            asyncio.run(request(config, "core.shutdown", {
                "expected_pid": info.pid, "expected_created_at": info.created_at,
            }))
        except ConnectionError:
            # 关闭响应可能随连接结束丢失；只有实际确认退出，才报告成功。
            pass
        process.wait(timeout=_STOP_TIMEOUT)
        _remove_record(info)
        print(f"stopped  pid={info.pid} (graceful shutdown)")
    except (OSError, RuntimeError, psutil.Error, TimeoutError) as exc:
        print(f"error: {exc}; PID record retained; process was not forcibly killed",
              file=sys.stderr)
        raise SystemExit(1) from exc
