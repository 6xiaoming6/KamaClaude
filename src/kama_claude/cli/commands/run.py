from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from kama_claude.cli.console import readline as _readline
from kama_claude.cli.permissions import PermissionPrompts
from kama_claude.core.config import KamaConfig
from kama_claude.core.permissions.policy import param_preview
from kama_claude.core.transport.socket_client import IpcError, SocketClient


class StdoutPrinter:
    # 接收 dict 格式的事件并将运行进度格式化打印到终端
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0
        self._finished_runs: set[str] = set()
        self._streamed: dict[str, str] = {}

    # 若当前行有未换行的 token，补一个换行符
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 根据事件 type 字段分发并格式化打印到 stdout/stderr
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            self._streamed[str(event.get("run_id", ""))] = ""
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._streamed[str(event.get("run_id", ""))] = ""
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "llm.token":
            run_id = str(event.get("run_id", ""))
            self._streamed[run_id] = self._streamed.get(run_id, "") + str(event.get("token", ""))
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            if event.get("tool_name") == "finish":
                return
            self._ensure_newline()
            params_str = param_preview(str(event.get("tool_name", "")), event.get("params", {}))
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            if event.get("tool_name") == "finish":
                return
            print(f"[tool] {event.get('tool_name', '')} ✓  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} ✗  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.interrupted":
            self._ensure_newline()
            print(
                f"[run] interrupted: {event.get('reason', '')}; "
                "reopen this session in TUI to recover",
                file=sys.stderr,
            )

        elif t == "run.finished":
            run_id = str(event.get("run_id", ""))
            if run_id in self._finished_runs:
                return
            self._finished_runs.add(run_id)
            self._ensure_newline()
            result = str(event.get("result") or "")
            if result and not self._streamed.get(run_id, "").rstrip().endswith(result.rstrip()):
                print(result)
            elapsed = time.monotonic() - self._run_start
            status = (
                "submitted" if result and event.get("status") == "success"
                else event.get("status", "")
            )
            print(f"[run] {status}  {event.get('steps')} steps  {elapsed:.1f}s")


# 一次性 Run 也使用会话订阅，子任务审批与其他会话严格隔离。
async def _run_async(goal: str, config: KamaConfig) -> int:
    client = SocketClient(config.host, config.port)
    printer, permissions = StdoutPrinter(), PermissionPrompts()
    finished, approval_received = asyncio.Event(), asyncio.Event()
    expected_run: str | None = None
    session_id = ""
    exit_code = 0
    tasks: list[asyncio.Task[Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal expected_run, exit_code
        if event.get("session_id", session_id) != session_id:
            return
        kind = event.get("type")
        if kind == "run.started" and not event.get("parent_run_id") and expected_run is None:
            expected_run = str(event["run_id"])
        if kind in {"permission.requested", "permission.granted", "permission.denied"}:
            printer._ensure_newline()
            permissions.handle(event)
            approval_received.set()
        if expected_run is None or event.get("run_id") != expected_run:
            return
        await printer.handle(event)
        if kind in {"run.finished", "run.interrupted"}:
            if kind == "run.interrupted" or event.get("status") != "success":
                exit_code = 1
            finished.set()

    async def answer_permissions() -> None:
        nonlocal exit_code
        input_closed = False
        while True:
            await approval_received.wait()
            approval_received.clear()
            while permissions.pending:
                request_id = next(iter(permissions.pending))
                try:
                    content = "n" if input_closed else await _readline("[approval] > ")
                except EOFError:
                    print("error: no approval input; denying pending operations", file=sys.stderr)
                    input_closed, exit_code, content = True, 1, "n"
                await permissions.answer(client, request_id, content)

    try:
        await client.connect()
        client.on_event(on_event)
        loop_task = asyncio.create_task(client.run_event_loop())
        tasks.append(loop_task)
        created = await client.send_command("session.create", {"mode": "one_shot"})
        session_id = str(created["session_id"])
        print(f"[session: {session_id}]")
        await client.send_command("event.subscribe", {
            "topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage", "permission.*"],
            "scope": f"session:{session_id}",
        })
        send_task = asyncio.create_task(client.send_command("session.send_message", {
            "session_id": session_id, "content": goal,
        }))
        approval_task = asyncio.create_task(answer_permissions())
        waiter = asyncio.create_task(finished.wait())
        tasks.extend([send_task, approval_task, waiter])
        watching = {send_task, approval_task, loop_task, waiter}
        while not finished.is_set():
            done, _ = await asyncio.wait(watching, return_when=asyncio.FIRST_COMPLETED)
            if finished.is_set():
                break
            if loop_task in done:
                raise ConnectionError("Core disconnected; reopen the session in TUI to inspect it")
            if approval_task in done:
                approval_task.result()
            if send_task in done:
                send_task.result()  # 初始化或执行请求失败不能被忽略。
                watching.discard(send_task)
        return exit_code
    except (IpcError, RuntimeError, OSError, TimeoutError) as exc:
        print(f"error: {exc}" + (f"; session={session_id}" if session_id else ""), file=sys.stderr)
        return 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


# 执行 kama run --goal "..." 命令
def cmd_run(goal: str, config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
