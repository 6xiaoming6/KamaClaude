from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from tests.unit.completion_support import submit_response


# 使用真实工具和持久化链路，仅以确定性模型选择工具，不验证真实模型决策能力。
async def test_repository_exploration_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/service.py").write_bytes(
        b"class Service:\n    def handle(self, value):\n        return value + 1\n"
    )
    bus, manager_permissions = EventBus(), PermissionManager()
    store = SessionStore(tmp_path / "sessions")
    operations = [
        ("find_files", {"pattern": "*.py", "path": "src"}),
        ("search_text", {"pattern": "handle", "path": "src", "output_mode": "files"}),
        ("get_file_outline", {"path": "src/service.py"}),
        ("search_symbol", {"name": "Service.handle", "path": "src"}),
        ("read_symbol", {"path": "src/service.py", "name": "Service.handle"}),
    ]
    calls: list[str] = []

    class ExploreProvider:
        async def chat(self, **kwargs: Any) -> LlmResponse:
            names = {s["name"] for s in kwargs["tool_schemas"]}
            assert {"get_file_outline", "search_symbol", "read_symbol"} <= names
            step = len(calls)
            if step:
                previous = json.loads(kwargs["messages"][-1]["content"][0]["content"])
                if step == 1:
                    assert previous["files"] == ["service.py"]
                elif step == 2:
                    assert previous["files"][0]["matching_lines"] == 1
                elif step in (3, 4):
                    assert any(s["qualified_name"] == "Service.handle" for s in previous["symbols"])
                elif step == 5:
                    assert previous["source"] == "    def handle(self, value):\n        return value + 1\n"
            if step == len(operations):
                calls.append("finish")
                return submit_response(LlmResponse("end_turn", text="Located Service.handle at lines 2-3."),
                                       kwargs["tool_schemas"], kwargs["run_id"], kwargs["step"])
            name, params = operations[step]
            calls.append(name)
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(str(step), name, params)],
                               usage=UsageStats(100, 10))

    manager = SessionManager(store, lambda: AgentRunner(
        KamaConfig(), provider=ExploreProvider(), bus=bus, permission_manager=manager_permissions,
    ), bus, permission_manager=manager_permissions)
    try:
        session = await manager.create("chat")
        await manager.send_message(session.id, "Find and explain Service.handle.")
        cp = store.checkpoints.load(session.id)
        assert cp.run is not None and cp.run.status == "success" and cp.run.step == 6
        history = store.read_history(session.id)
        results = [b for m in history if isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        assert len(results) == 6 and not any(b.get("is_error") for b in results)
        assert manager_permissions.pending_for_session(session.id) == []
    finally:
        await manager.shutdown()


async def test_repeated_search_warning_keeps_json_valid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "one.py").write_text("def f(): pass\n", encoding="utf-8")
    calls: list[dict] = []
    bus = EventBus()

    class RepeatingProvider:
        async def chat(self, **kwargs: Any) -> LlmResponse:
            if calls:
                previous = json.loads(kwargs["messages"][-1]["content"][0]["content"])
                assert previous["symbols"][0]["name"] == "f"
                if len(calls) == 3:
                    assert "No progress warning" in previous["notice"]
            if len(calls) == 4:
                return submit_response(LlmResponse("end_turn", text="stop repeated searching"),
                                       kwargs["tool_schemas"], kwargs["run_id"], kwargs["step"])
            calls.append(kwargs)
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(
                str(len(calls)), "search_symbol", {"name": "f", "path": "one.py"},
            )], usage=UsageStats(100, 10))

    runner = AgentRunner(KamaConfig(), provider=RepeatingProvider(), bus=bus)
    outcome = await runner.run_and_capture("Inspect f without an endless search.")
    assert outcome.status == "success" and len(calls) == 4
