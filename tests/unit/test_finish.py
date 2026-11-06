from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kama_claude.cli.commands.chat import ChatPrinter
from kama_claude.cli.commands.run import StdoutPrinter
from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.compact.history import is_user_request, validate_pairs
from kama_claude.core.completion import FINISH_REMINDER
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.finish import FinishParams, FinishTool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.tui.conversation import ConversationView
from kama_claude.tui.widgets.conversation import LLMStreamBlock
from tests.unit.loop_support import make_loop
from tests.unit.test_checkpoint_recovery import Crash, ProbeTool


class Model:
    # 不转换 end_turn，直接检验真实结束协议和修复上限。
    def __init__(self, *responses: LlmResponse) -> None:
        self.responses = iter(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls.append(deepcopy(kwargs["messages"]))
        return next(self.responses)


def submit(text: str = "已完成", identifier: str = "f") -> LlmResponse:
    return LlmResponse("tool_use", tool_calls=[ToolCallBlock(identifier, "finish", {"result": text})])


def context(max_steps: int = 5) -> ExecutionContext:
    return ExecutionContext(
        run=RunCheckpoint(
            run_id="run", goal="goal", model="test", workspace="",
            max_steps=max_steps, completion_mode="explicit",
        ), messages=[{"role": "user", "content": "goal"}],
    )


def registry() -> ToolRegistry:
    result = ToolRegistry()
    result.register(FinishTool())
    return result


@pytest.mark.parametrize("params", [{}, {"result": ""}, {"result": " \n"},
                                   {"result": 3}, {"result": "ok", "status": "success"}])
def test_finish_validates_only_nonempty_result(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FinishParams.model_validate(params)


@pytest.mark.parametrize("answer", ["已修改并通过验证", "你希望使用哪个数据库？", "全文" * 6000],
                         ids=["answer", "question", "long-answer"])
async def test_finish_is_paired_and_succeeds_on_last_step(answer: str) -> None:
    ctx, bus = context(max_steps=1), EventBus()
    events = []

    async def collect(event):
        events.append(event.type)

    bus.subscribe(collect)
    await make_loop(Model(submit(answer)), registry(), bus, context=ctx).run()
    assert ctx.run.status == "success" and ctx.run.result == answer
    assert ctx.run.phase == "finished" and ctx.run.tool_index == 1
    assert ctx.run.tools[0].status == "completed" and ctx.run.tools[0].output_prepared
    assert ctx.messages[-1]["content"][0]["content"] == answer
    assert validate_pairs(ctx.messages)
    assert events.index("tool.call_finished") < events.index("step.finished")


async def test_missing_finish_gets_one_persistable_reminder() -> None:
    ctx = context()
    model = Model(LlmResponse("end_turn", text="ordinary answer"), submit("submitted"))
    await make_loop(model, registry(), EventBus(), context=ctx).run()
    assert ctx.run.status == "success" and ctx.run.finish_reminders == 1
    assert len(model.calls) == 2
    assert model.calls[1][-1] == {"role": "user", "content": FINISH_REMINDER}
    assert not is_user_request(model.calls[1][-1])


async def test_missing_finish_repeated_fails_without_infinite_retry() -> None:
    ctx = context()
    model = Model(LlmResponse("end_turn", text="a"), LlmResponse("end_turn", text="b"))
    await make_loop(model, registry(), EventBus(), context=ctx).run()
    assert ctx.run.status == "failed" and ctx.run.reason == "missing_finish"
    assert ctx.run.step == 2 and len(model.calls) == 2


async def test_reminder_does_not_reset_step_budget() -> None:
    ctx = context(max_steps=1)
    model = Model(LlmResponse("end_turn", text="forgot finish"))
    await make_loop(model, registry(), EventBus(), context=ctx).run()
    assert ctx.run.reason == "exceeded_max_steps" and len(model.calls) == 1


@pytest.mark.parametrize("finish_first", [True, False])
async def test_mixed_batch_executes_no_business_tool(finish_first: bool) -> None:
    ctx, tools, probe = context(), registry(), ProbeTool("probe")
    tools.register(probe)
    calls = [ToolCallBlock("p", "probe", {}), submit().tool_calls[0]]
    if finish_first:
        calls.reverse()
    model = Model(LlmResponse("tool_use", tool_calls=calls), submit("corrected", "f2"))
    await make_loop(model, tools, EventBus(), context=ctx).run()
    assert probe.calls == 0 and ctx.run.result == "corrected"
    rejected = model.calls[1][-1]["content"]
    assert len(rejected) == 2 and all(b["is_error"] for b in rejected)
    assert validate_pairs(ctx.messages)


async def test_legacy_run_keeps_end_turn_behavior() -> None:
    ctx = context()
    old = ctx.run.model_dump()
    old.pop("completion_mode")
    ctx.run = RunCheckpoint.model_validate(old)
    model = Model(LlmResponse("end_turn", text="old answer"))
    await make_loop(model, ToolRegistry(), EventBus(), context=ctx).run()
    assert ctx.run.completion_mode == "legacy" and ctx.run.result == "old answer"
    assert ctx.run.status == "success" and len(model.calls) == 1


def session_manager(path: Path, model: Model, bus: EventBus) -> tuple[SessionStore, SessionManager]:
    store = SessionStore(path)
    return store, SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=model, bus=bus), bus)


async def test_question_ends_only_this_run_and_final_event_observes_saved_result(tmp_path: Path):
    bus = EventBus()
    store, manager = session_manager(tmp_path, Model(submit("哪个数据库？"), submit("用 SQLite")), bus)
    session = await manager.create("chat")
    saved = []

    async def collect(event):
        if event.type == "run.finished":
            saved.append((event.result, store.checkpoints.load(session.id)))

    bus.subscribe(collect)
    await manager.send_message(session.id, "保存数据")
    await manager.send_message(session.id, "SQLite")
    assert [result for result, _ in saved] == ["哪个数据库？", "用 SQLite"]
    assert len({cp.run.run_id for _, cp in saved}) == 2
    for result, cp in saved:
        assert cp.run.status == "success" and cp.run.result == result
        assert cp.session["status"] == "waiting_for_input"
    assert len(store.read_history(session.id)) == 6


@pytest.mark.parametrize("crash_after_commit", [False, True])
async def test_finish_save_failure_and_lost_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after_commit: bool,
) -> None:
    model, bus = Model(submit("saved answer")), EventBus()
    store, manager = session_manager(tmp_path, model, bus)
    session = await manager.create("chat")
    original = store.checkpoints.save
    finished = []

    async def collect(event):
        if event.type == "run.finished":
            finished.append(event)

    bus.subscribe(collect)

    def fail(cp):
        if cp.run and cp.run.status == "success":
            if crash_after_commit:
                original(cp)
                raise Crash()
            raise CheckpointError("controlled save failure")
        original(cp)

    monkeypatch.setattr(store.checkpoints, "save", fail)
    with pytest.raises(Crash if crash_after_commit else CheckpointError):
        await manager.send_message(session.id, "goal")
    assert not finished
    monkeypatch.setattr(store.checkpoints, "save", original)
    cp = store.checkpoints.load(session.id)
    assert (cp.run.status == "success") == crash_after_commit
    if crash_after_commit:
        _, restarted = session_manager(tmp_path, model, EventBus())
        opened = await restarted.open(session.id)
        assert opened["run"]["status"] == "success"
        assert len(model.calls) == 1
        assert store.read_history(session.id)[-1]["content"][0]["content"] == "saved answer"


async def test_pending_finish_recovers_without_another_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = Model(submit("ready to submit"))
    store, manager = session_manager(tmp_path, model, EventBus())
    session = await manager.create("chat")
    original = store.checkpoints.save

    def interrupt(cp):
        original(cp)
        if cp.run and cp.run.phase == "tools" and cp.run.tools[0].status == "pending":
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", interrupt)
    with pytest.raises(Crash):
        await manager.send_message(session.id, "goal")
    monkeypatch.setattr(store.checkpoints, "save", original)
    _, restarted = session_manager(tmp_path, model, EventBus())
    opened = await restarted.open(session.id)
    await restarted.resume(session.id, opened["revision"], "continue")
    task = restarted._tasks.get(session.id)
    if task:
        await task
    run = store.checkpoints.load(session.id).run
    assert run.status == "success" and run.result == "ready to submit"
    assert run.step == 1 and len(model.calls) == 1


async def test_cancel_during_finish_notification_keeps_committed_success(tmp_path: Path) -> None:
    bus = EventBus()
    store, manager = session_manager(tmp_path, Model(submit("committed")), bus)
    session = await manager.create("chat")

    async def cancel_notification(event):
        if event.type == "tool.call_finished" and event.tool_name == "finish":
            raise asyncio.CancelledError()

    bus.subscribe(cancel_notification)
    with pytest.raises(asyncio.CancelledError):
        await manager.send_message(session.id, "goal")
    cp = store.checkpoints.load(session.id)
    assert cp.run.status == "success" and cp.run.result == "committed"
    assert cp.session["status"] == "waiting_for_input"


def test_tui_shows_only_committed_finish_and_deduplicates_history():
    view, widgets = ConversationView(), []
    view.append = widgets.append
    call = {"type": "tool_use", "id": "f", "name": "finish", "input": {"result": "answer"}}
    view.render_history([{"role": "assistant", "run_id": "r", "content": [call]}])
    assert not widgets
    view.render_history([{"role": "user", "run_id": "r", "content": [{
        "type": "tool_result", "tool_use_id": "f", "content": "answer",
    }]}])
    view.handle_event({"type": "run.finished", "run_id": "r", "status": "success", "result": "answer"})
    view.handle_event({"type": "run.finished", "run_id": "r", "status": "success", "result": "answer"})
    assert [w._text for w in widgets if isinstance(w, LLMStreamBlock)] == ["answer"]


@pytest.mark.parametrize("printer_type", [StdoutPrinter, ChatPrinter])
async def test_cli_finish_is_a_reply_not_a_tool_dump(printer_type, capsys):
    printer = printer_type()
    await printer.handle({"type": "run.started", "run_id": "r", "goal": "goal"})
    await printer.handle({"type": "tool.call_started", "run_id": "r", "tool_name": "finish",
                   "params": {"result": "answer"}})
    await printer.handle({"type": "tool.call_finished", "run_id": "r", "tool_name": "finish",
                   "output": "answer"})
    assert "answer" not in capsys.readouterr().out
    final = {"type": "run.finished", "run_id": "r", "status": "success", "result": "answer"}
    await printer.handle(final)
    await printer.handle(final)
    output = capsys.readouterr().out
    assert output.count("answer") == 1 and "finish" not in output
