from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from kama_claude.core.assembly import create_agent_loop
from kama_claude.core.bus.events import SubagentFinishedEvent
from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.history import is_user_request, validate_pairs
from kama_claude.core.config import CompactionConfig, KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tracking import NOTICE_MARKER, SubagentTracker
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.registry import ToolRegistry


def state(tmp_path, parent="root"):
    return RunCheckpoint(run_id=parent, goal="inspect", model="test", workspace=str(tmp_path),
                         max_steps=10)


def finished(child="child", parent="root", status="success"):
    return SubagentFinishedEvent(run_id=child, parent_run_id=parent, status=status, ts="now")


@pytest.mark.parametrize("status", ["success", "failed", "interrupted"])
async def test_completion_is_queued_and_delivered_once_without_collecting(tmp_path, status):
    run = state(tmp_path)
    run.subagents["child"] = "background"
    tracker = SubagentTracker(run)
    await tracker.handle_event(finished(status=status))
    messages = [{"role": "user", "content": "real user input"}]
    assert len(messages) == 1 and not run.subagent_notifications["child"].delivered
    assert tracker.deliver_notifications(messages)
    assert status in messages[-1]["content"] and "agent_result" in messages[-1]["content"]
    assert "child" in messages[-1]["content"] and not is_user_request(messages[-1])
    assert tracker.finish_error() is not None  # 通知不产生交接凭据。
    await tracker.handle_event(finished(status=status))
    assert not tracker.deliver_notifications(messages) and len(messages) == 2
    assert run.subagents["child"] == ("unknown" if status == "interrupted" else "background")


async def test_collected_results_remove_pending_notices_and_ignore_late_events(tmp_path):
    run = state(tmp_path)
    run.subagents["child"] = "running"
    tracker = SubagentTracker(run)
    await tracker.handle_event(finished())
    tracker.record_result(ToolResult("handoff", collected_subagents=["child"]))
    await tracker.handle_event(finished())
    assert not run.subagent_notifications and run.subagents["child"] == "collected"
    assert not tracker.deliver_notifications([]) and tracker.finish_error() is None


async def test_only_direct_parent_receives_notice_and_batch_is_one_message(tmp_path):
    root, child = state(tmp_path), state(tmp_path, "child")
    root.subagents = {"child": "background", "grandchild": "background"}
    child.subagents = {"grandchild": "background"}
    trackers = [SubagentTracker(root), SubagentTracker(child), SubagentTracker(state(tmp_path, "other"))]
    for tracker in trackers:
        await tracker.handle_event(finished("grandchild", "child"))
    assert not root.subagent_notifications
    assert set(child.subagent_notifications) == {"grandchild"}
    assert not trackers[-1].run.subagent_notifications
    root.subagents["second"] = "background"
    await trackers[0].handle_event(finished())
    await trackers[0].handle_event(finished("second", status="failed"))
    messages = []
    assert trackers[0].deliver_notifications(messages) and len(messages) == 1
    assert "`child`" in messages[0]["content"] and "`second`" in messages[0]["content"]


async def test_queue_and_delivery_flag_survive_checkpoint_round_trip(tmp_path):
    run = state(tmp_path)
    run.subagents["child"] = "background"
    await SubagentTracker(run).handle_event(finished())
    restored = RunCheckpoint.model_validate_json(run.model_dump_json())
    messages = []
    assert SubagentTracker(restored).deliver_notifications(messages)
    replayed = RunCheckpoint.model_validate_json(restored.model_dump_json())
    assert not SubagentTracker(replayed).deliver_notifications(messages)
    assert len(messages) == 1


async def test_notice_commit_failure_prevents_model_call(tmp_path):
    from unittest.mock import Mock

    from tests.unit.test_progress import RepeatingModel

    run = state(tmp_path)
    run.subagents["child"] = "background"
    run.phase = "llm"
    tracker = SubagentTracker(run)
    await tracker.handle_event(finished())
    persistence = Mock()
    persistence.checkpoint.token_estimation = {}
    persistence.save.side_effect = CheckpointError("notice save failed")
    context = ExecutionContext(run=run)
    model = RepeatingModel()
    loop = create_agent_loop(model, ToolRegistry(), EventBus(), context=context,
                             compaction=CompactionConfig(auto_threshold=0), subagents=tracker,
                             persistence=persistence)
    with pytest.raises(CheckpointError, match="notice save failed"):
        await loop.run()
    assert not model.calls


async def test_real_parent_receives_short_notice_then_collects_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bus, registry = EventBus(), BackgroundTaskRegistry()
    completion = asyncio.Event()
    child_ids = []

    async def observe(event):
        if event.type == "subagent.started":
            child_ids.append(event.run_id)
        elif event.type == "subagent.finished":
            completion.set()

    bus.subscribe(observe)

    class Model:
        def __init__(self):
            self.root = ""
            self.calls = []

        async def chat(self, **kwargs):
            self.root = self.root or kwargs["run_id"]
            self.calls.append(deepcopy(kwargs))
            if kwargs["run_id"] != self.root:
                call = ToolCallBlock("child-finish", "finish", {"result": "PRIVATE full report"})
            elif kwargs["step"] == 1:
                call = ToolCallBlock("spawn", "spawn_agent", {
                    "description": "inspect", "prompt": "inspect", "run_in_background": True,
                })
            elif kwargs["step"] == 2:
                # 完成也可能发生在父模型请求期间；提醒在之后的请求准备边界投递。
                await asyncio.wait_for(completion.wait(), timeout=5)
                call = ToolCallBlock("list", "list_dir", {"path": "."})
            elif kwargs["step"] == 3:
                call = ToolCallBlock("collect", "agent_result", {"run_ids": child_ids})
            else:
                call = ToolCallBlock("finish", "finish", {"result": "done"})
            return LlmResponse("tool_use", tool_calls=[call], usage=UsageStats(10, 5))

    model = Model()
    config = KamaConfig()
    config.compaction.auto_threshold = 0
    store = SessionStore(tmp_path / "sessions")
    sessions = SessionManager(store, lambda: AgentRunner(
        config, bus=bus, provider=model, task_registry=registry,
    ), bus)
    session = await sessions.create("chat")
    await sessions.send_message(session.id, "inspect with help")
    cp = store.checkpoints.load(session.id)
    assert cp.run.status == "success" and validate_pairs(cp.messages)
    parent_calls = [call for call in model.calls if call["run_id"] == model.root]
    third = parent_calls[2]
    notices = [message for message in third["messages"]
               if str(message.get("content", "")).startswith(NOTICE_MARKER)]
    assert len(notices) == 1 and child_ids[0] in notices[0]["content"]
    assert "success" in notices[0]["content"] and "PRIVATE full report" not in str(third["messages"])
    assert cp.run.subagents[child_ids[0]] == "collected"
    assert not cp.run.subagent_notifications
    assert sum(str(message.get("content", "")).startswith(NOTICE_MARKER)
               for message in cp.messages) == 1
    assert not any(str(message.get("content", "")).startswith(NOTICE_MARKER)
                   for message in store.read_history(session.id))
