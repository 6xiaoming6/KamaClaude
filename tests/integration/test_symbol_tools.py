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


@pytest.mark.parametrize("filename,source,name,body", [
    ("service.py", "class Service:\n    def handle(self, value):\n        return value + 1\n",
     "Service.handle", "    def handle(self, value):\n        return value + 1\n"),
    ("service.cpp", "class Service {\npublic:\n int handle(int value) {\n  return value + 1;\n }\n};\n",
     "Service::handle", " int handle(int value) {\n  return value + 1;\n }\n"),
    ("Service.java", "class Service {\n int handle(int value) {\n  return value + 1;\n }\n}\n",
     "Service.handle", " int handle(int value) {\n  return value + 1;\n }\n"),
    ("service.rs", "struct Service;\nimpl Service {\n fn handle(&self, value: i32) -> i32 {\n  value + 1\n }\n}\n",
     "Service::handle", " fn handle(&self, value: i32) -> i32 {\n  value + 1\n }\n"),
    ("service.go", "package service\ntype Service struct{}\nfunc (s *Service) handle(value int) int {\n return value + 1\n}\n",
     "Service.handle", "func (s *Service) handle(value int) int {\n return value + 1\n}\n"),
])
# 功能：验证主 Agent 使用新增语言的真实工具完成定位、阅读和完整历史保存。
# 设计：仅用假模型选择工具，解析、权限、Runner 和检查点真实执行，不声称验证模型决策。
async def test_repository_exploration_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, source: str, name: str, body: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    target = "src/" + filename
    (tmp_path / target).write_bytes(source.encode("utf-8"))
    bus, manager_permissions = EventBus(), PermissionManager()
    store = SessionStore(tmp_path / "sessions")
    operations = [
        ("find_files", {"pattern": "*" + Path(filename).suffix, "path": "src"}),
        ("search_text", {"pattern": "handle", "path": "src", "output_mode": "files"}),
        ("get_file_outline", {"path": target}),
        ("read_symbol", {"path": target, "name": name}),
    ]
    calls: list[str] = []

    class ExploreProvider:
        # 根据上一次真实工具返回推进固定任务，避免仅测试孤立解析器。
        async def chat(self, **kwargs: Any) -> LlmResponse:
            names = {s["name"] for s in kwargs["tool_schemas"]}
            assert {"get_file_outline", "read_symbol"} <= names
            assert "search_symbol" not in names
            step = len(calls)
            if step:
                previous = json.loads(kwargs["messages"][-1]["content"][0]["content"])
                if step == 1:
                    assert previous["files"] == [filename]
                elif step == 2:
                    assert previous["files"][0]["matching_lines"] == 1
                elif step == 3:
                    assert any(s["qualified_name"] == name for s in previous["symbols"])
                elif step == 4:
                    assert previous["source"] == body
            if step == len(operations):
                calls.append("finish")
                return submit_response(LlmResponse("end_turn", text=f"Located {name}."),
                                       kwargs["tool_schemas"], kwargs["run_id"], kwargs["step"])
            tool_name, params = operations[step]
            calls.append(tool_name)
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock(str(step), tool_name, params)],
                               usage=UsageStats(100, 10))

    manager = SessionManager(store, lambda: AgentRunner(
        KamaConfig(), provider=ExploreProvider(), bus=bus, permission_manager=manager_permissions,
    ), bus, permission_manager=manager_permissions)
    try:
        session = await manager.create("chat")
        await manager.send_message(session.id, "Find and explain Service.handle.")
        cp = store.checkpoints.load(session.id)
        assert cp.run is not None and cp.run.status == "success" and cp.run.step == 5
        history = store.read_history(session.id)
        results = [b for m in history if isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        assert len(results) == 5 and not any(b.get("is_error") for b in results)
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
                str(len(calls)), "get_file_outline", {"path": "one.py"},
            )], usage=UsageStats(100, 10))

    runner = AgentRunner(KamaConfig(), provider=RepeatingProvider(), bus=bus)
    outcome = await runner.run_and_capture("Inspect f without an endless search.")
    assert outcome.status == "success" and len(calls) == 4
