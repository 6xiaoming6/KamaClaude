from __future__ import annotations

import asyncio
import sys
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending_permissions: dict[str, dict[str, Any]] = {}

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 同时有多个审批时始终展示队首；输入不能批准最后打印的另一项操作。
    def show_pending_permission(self) -> None:
        if not self.pending_permissions:
            return
        request_id = next(iter(self.pending_permissions))
        event = self.pending_permissions[request_id]
        tool_name = str(event.get("tool_name", ""))
        print(f"[permission {request_id}] {tool_name}  {event.get('param_preview', '')}")
        if tool_name == "bash":
            print(f"  command: {(event.get('params') or {}).get('command', '')}")
            print(f"  cwd: {event.get('workspace', '')} (local shell, not sandboxed)")
        if event.get("resolved_path"):
            print(f"  target: {event['resolved_path']}")
        print(f"  reason: {event.get('reason', '')}")
        print(f"  scope: {event.get('approval_scope', '仅本次操作')}")
        allowed = event.get("allowed_decisions") or ["allow_once", "deny_once"]
        print("  " + "  ".join(
            f"{key}={value}" for key, value in _DECISION_MAP.items() if value in allowed
        ))

    def resolve_permission(self, request_id: str) -> None:
        if self.pending_permissions.pop(request_id, None) is not None:
            self.show_pending_permission()

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            self._ensure_newline()
            request_id = str(event["request_id"])
            self.pending_permissions[request_id] = event
            self.show_pending_permission()
        elif t in {"permission.denied", "permission.granted"}:
            self.resolve_permission(str(event.get("request_id", "")))
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")


# 在线程池中读取 stdin，避免阻塞 socket event loop
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


# 异步核心：创建 chat session，循环读取用户输入并发送到 daemon；权限请求时优先处理审批
async def _chat_async(config: KamaConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())
    send_task: asyncio.Task[None] | None = None

    try:
        created = await client.send_command("session.create", {"mode": "chat"})
        session_id = str(created["session_id"])
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": f"session:{session_id}",
            },
        )
        print(f"[session: {session_id}]")

        # 运行请求可能等待整轮结束；单独等待，主输入循环才能接收审批回答。
        async def send_message(content: str) -> None:
            try:
                await client.send_command(
                    "session.send_message", {"session_id": session_id, "content": content},
                )
            except (IpcError, RuntimeError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break
            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending_permissions:
                request_id = next(iter(printer.pending_permissions))
                request = printer.pending_permissions[request_id]
                decision = _DECISION_MAP.get(content.lower())
                if decision not in request.get("allowed_decisions", []):
                    print("  choose one of the decisions shown for this request")
                    continue
                try:
                    await client.send_command("permission.respond", {
                        "request_id": request_id, "decision": decision,
                        "session_id": request["session_id"], "run_id": request["run_id"],
                    })
                except IpcError as exc:
                    print(f"  approval not submitted: {exc}")
                    continue
                printer.resolve_permission(request_id)
                continue

            if send_task is not None and not send_task.done():
                print("  current run is still executing; wait for completion or approval")
                continue
            send_task = asyncio.create_task(send_message(content))

        await client.send_command("session.close", {"session_id": session_id})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if send_task is not None:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    return 0


# 执行 kama chat 命令
def cmd_chat(config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
