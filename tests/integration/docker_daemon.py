from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from kama_claude.core.app import CoreApp
from kama_claude.core.config import KamaConfig
from kama_claude.core.execution.owner import SandboxOwner, cleanup_orphans
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_server import SocketServer


class Provider:
    # 只用明确的两条命令控制恢复位置，不调用任何模型 API。
    async def chat(self, **kwargs: Any) -> LlmResponse:
        messages = kwargs["messages"]
        if any(isinstance(m.get("content"), list) and any(
            b.get("type") == "tool_result" for b in m["content"]
        ) for m in messages):
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                "finish", "finish", {"result": "Docker work completed"},
            )])
        first = "from pathlib import Path; p=Path('effects.txt'); p.open('a').write('a\\n')"
        second = (
            "from pathlib import Path\nimport time\n"
            "Path('effects.txt').open('a').write('b\\n')\n"
            "Path('b-started').touch()\n"
            "while not Path('release-b').exists(): time.sleep(0.02)\n"
            "print('b complete')"
        )
        return LlmResponse("tool_use", tool_calls=[
            ToolCallBlock("a", "bash", {"command": "python -c " + shlex.quote(first)}),
            ToolCallBlock("b", "bash", {"command": "python -c " + shlex.quote(second)}),
        ])


# 运行实际 Handler、TCP、权限、检查点及 Docker，只替换模型回复。
async def main(root: Path, port: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    config = KamaConfig()
    app = CoreApp()
    app._broadcaster = IpcEventBroadcaster()
    app._bus.subscribe(app._broadcaster.handle)
    permissions = PermissionManager()
    app._permission_manager = permissions
    store = SessionStore(root / "sessions")
    owner = SandboxOwner.for_core(root / "sessions", "127.0.0.1", port)
    await cleanup_orphans(owner)
    app._sessions = SessionManager(
        store, lambda: AgentRunner(config, bus=app._bus, provider=Provider(),
                                  permission_manager=permissions, sandbox_owner=owner),
        app._bus, permission_manager=permissions,
    )
    server = SocketServer("127.0.0.1", port, app._broadcaster)
    for method, handler in (
        ("session.create", app._session_create_handler),
        ("session.open", app._session_open_handler),
        ("session.send_message", app._session_send_handler),
        ("session.resume", app._session_resume_handler),
        ("session.set_permission_mode", app._session_permission_mode_handler),
        ("event.subscribe", app._subscribe_handler),
    ):
        server.register(method, handler)
    await server.start()
    (root / "ready.json").write_text(json.dumps({"scope": owner.scope}), encoding="utf-8")
    try:
        await asyncio.Event().wait()
    finally:
        await app._sessions.shutdown()
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve(), int(sys.argv[2])))
