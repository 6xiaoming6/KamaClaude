from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError, SocketClient


class StdoutPrinter:
    # 接收 dict 格式的事件并将运行进度格式化打印到终端
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0

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
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get('tool_name', '')} ✓  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} ✗  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.get('status', '')}  {event.get('steps')} steps  {elapsed:.1f}s")


# 异步核心：连接 daemon，订阅事件，触发 run，等待 run.finished
async def _run_async(goal: str, config: KamaConfig) -> int:
    # 和core建立 TCP 连接
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
    finished = asyncio.Event()
    exit_code = 0

    # 事件回调函数，core的事件发布后都会经过这个函数
    async def on_event(event: dict[str, Any]) -> None:
        nonlocal exit_code
        # 先打印这个事件
        await printer.handle(event)
        # 如果事件是任务完成事件的话，就把finished等待设为完成，就会结束_run_async
        if event.get("type") == "run.finished":
            if event.get("status") != "success":
                exit_code = 1
            finished.set()
    # 注册事件回调函数，client收到事件后，交给这个函数处理
    client.on_event(on_event)
    # 创建任务持续读取core发来的数据
    # 一定要先创建循环监听服务器消息再send_command，不然前面的请求可能会接收不到响应

    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        # 先向core发送消息注册订阅
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
                "scope": "global",
            },
        )
        # 再启动运行，AgentRunner在后端启动，前端cli只负责发送命令和查看进度
        await client.send_command("agent.run", {"goal": goal})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    # 等待任务完成
    await finished.wait()
    # 任务完成后就不需要循环监听后端的消息了
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    await client.close()
    return exit_code


# 执行 kama run --goal "..." 命令
def cmd_run(goal: str, config: KamaConfig) -> None:
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
