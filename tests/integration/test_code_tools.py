from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from tests.unit.test_command_executor import python_command


# 功能：验证真实工具完成查找、搜索、分页读取、局部编辑和命令验证整条链
# 设计：只替换模型决策，真实执行审批、文件提交、进程验证、检查点与完整历史
async def test_locate_read_edit_verify_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "settings.py"
    target.write_text("timeout = 30\n", encoding="utf-8")
    calls: list[str] = []
    bus, permissions = EventBus(), PermissionManager()
    approvals: list[str] = []

    # 只批准会产生修改或启动命令的两次操作
    async def approve(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            approvals.append(event.tool_name)
            permissions.respond(event.tool_use_id, "allow_once")

    bus.subscribe(approve)

    class RepairProvider:
        # 根据上一步真实结果继续，不接受模型调用之外的文件读写捷径
        async def chat(self, **kwargs: Any) -> LlmResponse:
            step = len(calls)
            if step:
                previous = kwargs["messages"][-1]["content"][0]["content"]
                if step == 1:
                    assert json.loads(previous)["files"] == ["settings.py"]
                elif step == 2:
                    assert json.loads(previous)["matches"][0]["line"] == 1
                elif step == 3:
                    assert "1 | timeout = 30" in previous
                elif step == 4:
                    assert "+timeout = 60" in json.loads(previous)["diff"]
                elif step == 5:
                    assert "verified" in previous
            command = python_command(
                "from pathlib import Path; "
                "assert 'timeout = 60' in Path('settings.py').read_text(); print('verified')"
            )
            operations = [
                ("find_files", {"pattern": "*.py"}),
                ("search_text", {"pattern": "timeout", "include": "*.py"}),
                ("read_file", {"path": "settings.py", "max_lines": 10}),
                ("edit_file", {"path": "settings.py", "old_text": "timeout = 30", "new_text": "timeout = 60"}),
                ("bash", {"command": command}),
            ]
            if step == len(operations):
                calls.append("finished")
                return LlmResponse("end_turn", text="verified", usage=UsageStats(100, 10))
            name, params = operations[step]
            calls.append(name)
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                f"call-{step}", name, params,
            )], usage=UsageStats(100, 10))

    config = KamaConfig()
    registry = BackgroundTaskRegistry()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, lambda: AgentRunner(
            config, provider=RepairProvider(), bus=bus, permission_manager=permissions,
            task_registry=registry,
        ), bus, permission_manager=permissions, task_registry=registry,
    )
    session = await manager.create("chat")
    await manager.send_message(session.id, "Change the default timeout to 60 and verify it.")
    checkpoint = store.checkpoints.load(session.id)
    assert checkpoint.run is not None and checkpoint.run.status == "success"
    assert checkpoint.run.step == 6 and checkpoint.run.result == "verified"
    assert target.read_text(encoding="utf-8") == "timeout = 60\n"
    assert approvals == ["edit_file", "bash"]
    history = store.read_history(session.id)
    results = [block for message in history if isinstance(message["content"], list)
               for block in message["content"] if block.get("type") == "tool_result"]
    assert len(results) == 5 and not any(result.get("is_error") for result in results)
    await manager.shutdown()
