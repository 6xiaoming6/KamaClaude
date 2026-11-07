from __future__ import annotations

import asyncio
import sys
from typing import Any

from kama_claude.cli.console import readline as _readline
from kama_claude.cli.permissions import PermissionPrompts
from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self._finished_runs: set[str] = set()
        self._streamed: dict[str, str] = {}
        self.permissions = PermissionPrompts()
        self.pending_permissions = self.permissions.pending
        self.interrupted = asyncio.Event()
        self._root_run: str | None = None

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    def resolve_permission(self, request_id: str) -> None:
        self.permissions.resolve(request_id)

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t in ("run.started", "step.started"):
            self._streamed[str(event.get("run_id", ""))] = ""
            if (t == "run.started" and not event.get("parent_run_id")
                    and (self._root_run is None or self._root_run in self._finished_runs)):
                self._root_run = str(event.get("run_id", ""))
        elif t == "llm.token":
            run_id = str(event.get("run_id", ""))
            self._streamed[run_id] = self._streamed.get(run_id, "") + str(event.get("token", ""))
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            if event.get("tool_name") == "finish":
                return
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "tool.call_failed":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')} failed: {event.get('error_message', '')}",
                  file=sys.stderr)
        elif t == "permission.requested":
            self._ensure_newline()
            self.permissions.handle(event)
        elif t in {"permission.denied", "permission.granted"}:
            self.resolve_permission(str(event.get("request_id", "")))
        elif t == "run.interrupted":
            self._ensure_newline()
            print(f"[run] interrupted: {event.get('reason', '')}; reopen this session in TUI",
                  file=sys.stderr)
            if self._root_run is None or event.get("run_id") == self._root_run:
                self.interrupted.set()
        elif t == "run.finished":
            run_id = str(event.get("run_id", ""))
            if run_id in self._finished_runs:
                return
            self._finished_runs.add(run_id)
            result = str(event.get("result") or "")
            self._ensure_newline()
            if result and not self._streamed.get(run_id, "").rstrip().endswith(result.rstrip()):
                print(result)
            if event.get("status") != "success":
                print(f"[run] {event.get('status', 'failed')}: {event.get('reason', '')}",
                      file=sys.stderr)
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")


# 事件回调只展示并更新状态；RPC、用户输入和连接结束在主协程协调。
async def _chat_async(config: KamaConfig) -> int:
    client = SocketClient(config.host, config.port)
    loop_task: asyncio.Task[None] | None = None
    send_task: asyncio.Task[None] | None = None
    input_task: asyncio.Task[str] | None = None
    interrupted_task: asyncio.Task[bool] | None = None
    session_id = ""
    printer = ChatPrinter()
    try:
        await client.connect()
        client.on_event(printer.handle)
        loop_task = asyncio.create_task(client.run_event_loop())
        created = await client.send_command("session.create", {"mode": "chat"})
        session_id = str(created["session_id"])
        await client.send_command("event.subscribe", {
            "topics": ["session.*", "run.*", "step.started", "tool.*", "llm.token", "permission.*"],
            "scope": f"session:{session_id}",
        })
        print(f"[session: {session_id}]")
        interrupted_task = asyncio.create_task(printer.interrupted.wait())

        async def send_message(content: str) -> None:
            try:
                await client.send_command("session.send_message", {
                    "session_id": session_id, "content": content,
                })
            except (IpcError, RuntimeError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)

        while True:
            # 审批已展示时捕获其 ID；输入期间新出现的审批由下方队首规则处理。
            request_id = next(iter(printer.pending_permissions), None)
            input_task = asyncio.create_task(_readline("> "))
            done, _ = await asyncio.wait(
                {input_task, loop_task, interrupted_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if loop_task in done:
                print(f"error: Core disconnected; session={session_id}; reopen it in TUI",
                      file=sys.stderr)
                return 1
            if interrupted_task in done:
                print(f"[session: {session_id}] preserved for recovery", file=sys.stderr)
                return 1
            try:
                content = input_task.result().strip()
            except EOFError:
                # 保留原有 EOF 关闭会话语义；Ctrl+C 和断线仅断开前端。
                print("[closing session]")
                await asyncio.wait_for(client.send_command("session.close", {
                    "session_id": session_id,
                }), 5)
                return 0
            if not content:
                continue
            if request_id is not None or printer.pending_permissions:
                request_id = request_id or next(iter(printer.pending_permissions))
                await printer.permissions.answer(client, request_id, content)
                continue
            if send_task is not None and not send_task.done():
                print("  current run is still executing; wait for completion or approval")
                continue
            send_task = asyncio.create_task(send_message(content))
    except (IpcError, RuntimeError, OSError, TimeoutError) as exc:
        print(f"error: {exc}" + (f"; session={session_id}" if session_id else ""), file=sys.stderr)
        return 1
    finally:
        tasks = [task for task in (input_task, send_task, interrupted_task, loop_task)
                 if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


# 执行 kama chat 命令
def cmd_chat(config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
