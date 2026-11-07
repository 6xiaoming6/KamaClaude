from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from kama_claude.core.app import CoreApp
from kama_claude.core.config import KamaConfig
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin.finish import FinishTool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_server import SocketServer


class FileProbe(BaseTool):
    name = "probe"
    description = "controlled file side effect"
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    # 将受控输出限定在测试临时目录
    def __init__(self, root: Path) -> None:
        self.root = root

    # 先写入可观察副作用，再等待测试释放第二个工具
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        name = str(params["name"])
        with (self.root / "effects.txt").open("a", encoding="utf-8") as f:
            f.write(name + "\n")
            f.flush()
            os.fsync(f.fileno())
        if name == "b":
            (self.root / "b-started").touch()
            while not (self.root / "release-b").exists():
                await asyncio.sleep(0.02)
        if name == "a" and (self.root / "long-output").exists():
            return ToolResult(content="HEAD" + "x" * 10_000 + "TAIL")
        return ToolResult(content=name + " complete")


class Provider:
    # 保存测试控制目录，不读取真实模型配置或密钥
    def __init__(self, root: Path) -> None:
        self.root = root

    # 根据实际保存的上下文返回固定工具请求或最终结果
    async def chat(self, **kwargs: Any) -> LlmResponse:
        if kwargs.get("run_id") == "compact":
            with (self.root / "summary-calls.txt").open("a", encoding="utf-8") as file:
                file.write("summary\n")
            return LlmResponse("end_turn", text="S9 handoff: old work completed; continue the task.")
        if (self.root / "s9-mode").exists() or (self.root / "s9-program-mode").exists():
            (self.root / "s9-model-waiting").touch()
            while not (self.root / "s9-release-model").exists():
                await asyncio.sleep(0.02)
        messages = kwargs["messages"]
        last = messages[-1]["content"]
        if isinstance(last, list) and any(b.get("type") == "tool_result" for b in last):
            if any(schema["name"] == "finish" for schema in kwargs["tool_schemas"]):
                return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                    "finish-test", "finish", {"result": "done"},
                )])
            return LlmResponse("end_turn", text="done")
        return LlmResponse(
            "tool_use",
            tool_calls=[
                ToolCallBlock("tool-a", "probe", {"name": "a"}),
                ToolCallBlock("tool-b", "probe", {"name": "b"}),
            ],
        )


# 运行真实 Core handler、检查点与 TCP，只替换模型和有副作用工具
async def main(root: Path, port: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if (root / "long-output").exists():
        from kama_claude.core.compact import budget

        original_archive = budget._archive_output

        def controlled_archive(path: Path, text: str) -> None:
            if (root / "archive-fail").exists():
                raise OSError("controlled archive failure")
            original_archive(path, text)

        budget._archive_output = controlled_archive
    app = CoreApp()
    app._broadcaster = IpcEventBroadcaster()
    app._bus.subscribe(app._broadcaster.handle)

    class Runner(AgentRunner):
        # 每个 Run 仍由真实 Runner 组装，只注册受控实验工具
        def _build_registry(self, task_manager: Any, **kwargs: Any) -> ToolRegistry:
            registry = ToolRegistry()
            registry.register(FileProbe(root))
            run = kwargs.get("run")
            if run is not None and run.completion_mode == "explicit":
                registry.register(FinishTool())
            return registry

    config = KamaConfig()
    if (root / "long-output").exists():
        # 故障注入用小阈值触发归档，不依赖日常开发配置的默认限长。
        config.compaction.tool_result_limit = 8_000
        config.compaction.tool_result_keep = 4_000
    if (root / "s9-mode").exists():
        # 此用例用短历史验证压缩后重启，固定小窗口以控制触发条件。
        config.compaction.context_window_tokens = 65_536
        config.compaction.auto_threshold = 0.05
    if (root / "s9-program-mode").exists():
        config.compaction.context_window_tokens = 65_536
        config.compaction.auto_threshold = 0.05
        config.compaction.recent_history_ratio = 2_000 / 65_536
        config.compaction.output_reserve_ratio = 512 / 65_536
        config.compaction.summary_max_ratio = 256 / 65_536
        config.compaction.summary_target_ratio = 128 / 65_536
    app._sessions = SessionManager(
        SessionStore(root / "sessions"),
        lambda: Runner(config, provider=Provider(root), bus=app._bus),
        app._bus,
    )
    server = SocketServer("127.0.0.1", port, app._broadcaster)
    for method, handler in {
        "session.create": app._session_create_handler,
        "session.send_message": app._session_send_handler,
        "session.list": app._session_list_handler,
        "session.open": app._session_open_handler,
        "session.get_history": app._session_history_handler,
        "session.resume": app._session_resume_handler,
        "session.close": app._session_close_handler,
    }.items():
        server.register(method, handler)
    await server.start()
    (root / "ready.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    try:
        await asyncio.Event().wait()
    finally:
        server.begin_shutdown()
        try:
            await app._sessions.shutdown()
        finally:
            await server.stop()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), int(sys.argv[2])))
