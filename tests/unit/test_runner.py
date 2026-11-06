from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import RunStartedEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore

# --- mock provider -----------------------------------------------------------


class _EndTurnProvider:
    """Immediately returns end_turn; no API calls made."""

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        max_output_tokens: int = 8_192,
    ) -> LlmResponse:
        return LlmResponse(stop_reason="end_turn", text="done")


class _LoopingProvider:
    """Always returns tool_use with an unknown tool to exhaust max_steps."""

    def __init__(self) -> None:
        self._call = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        max_output_tokens: int = 8_192,
    ) -> LlmResponse:
        self._call += 1
        tc = ToolCallBlock(id=f"t{self._call}", name="unknown_tool", input={})
        return LlmResponse(stop_reason="tool_use", tool_calls=[tc])


class _CapturingProvider:
    # 初始化捕获型 provider，保存固定响应
    def __init__(self, response: LlmResponse) -> None:
        self.response = response
        self.messages: list[dict[str, object]] = []
        self.system: str | None = None

    # 捕获本次 LLM 调用的 messages 和 system prompt
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        max_output_tokens: int = 8_192,
    ) -> LlmResponse:
        self.messages = [dict(m) for m in messages]
        self.system = system
        return self.response


# --- helpers -----------------------------------------------------------------


def _config(max_steps: int = 5) -> KamaConfig:
    cfg = KamaConfig()
    cfg.agent.max_steps = max_steps
    return cfg


async def _run(
    goal: str = "test goal",
    *,
    provider: object | None = None,
    config: KamaConfig | None = None,
    tmp_path: Path,
) -> list[BaseModel]:
    collected: list[BaseModel] = []

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    cfg = config or _config()
    runner = AgentRunner(
        cfg,
        provider=provider or _EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[_collect],
        runs_dir=tmp_path,
    )
    await runner.run(goal)
    return collected


# --- tests -------------------------------------------------------------------


# 功能：验证 run 开始时发布携带正确 goal 的 run.started 事件
# 设计：用 extra_handlers 收集事件，而非从 events.jsonl 读取，避免文件 I/O 耦合；聚焦 runner 层的事件发布职责
async def test_run_started_event_published(tmp_path: Path) -> None:
    events = await _run(goal="my goal", tmp_path=tmp_path)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "run.started" in types
    started = next(e for e in events if e.type == "run.started")  # type: ignore[attr-defined]
    assert started.goal == "my goal"  # type: ignore[attr-defined]


# 功能：验证成功完成时发布 status=success 的 run.finished 事件
# 设计：EndTurnProvider 触发最短成功路径，聚焦 runner 层对任何终止路径都能保证发布 finished 事件
async def test_run_finished_event_published_on_success(tmp_path: Path) -> None:
    events = await _run(tmp_path=tmp_path)
    finished = next(
        (e for e in events if e.type == "run.finished"), None  # type: ignore[attr-defined]
    )
    assert finished is not None
    assert finished.status == "success"  # type: ignore[attr-defined]


# 功能：验证步数耗尽时 run.finished 携带 failed 状态和正确的失败原因
# 设计：LoopingProvider + max_steps=2 触发失败路径，确认 runner 在失败终止路径同样发布 finished 事件
async def test_run_finished_event_published_on_max_steps(tmp_path: Path) -> None:
    events = await _run(
        provider=_LoopingProvider(),
        config=_config(max_steps=2),
        tmp_path=tmp_path,
    )
    finished = next(e for e in events if e.type == "run.finished")  # type: ignore[attr-defined]
    assert finished.status == "failed"  # type: ignore[attr-defined]
    assert finished.reason == "exceeded_max_steps"  # type: ignore[attr-defined]


# 功能：验证 events.jsonl 第一行为 run.started、最后一行为 run.finished
# 设计：从 tmp_path 递归查找 events.jsonl 并按行解析，因为 events.jsonl 是 S1 的核心产物，首尾事件是完整性的最低要求
async def test_events_jsonl_created_with_started_and_finished(tmp_path: Path) -> None:
    await _run(tmp_path=tmp_path)
    jsonl_files = list(tmp_path.rglob("events.jsonl"))
    assert len(jsonl_files) == 1
    lines = [json.loads(ln) for ln in jsonl_files[0].read_text().splitlines() if ln]
    event_types = [e["type"] for e in lines]
    assert event_types[0] == "run.started"
    assert event_types[-1] == "run.finished"


# 功能：验证 runner 在 runs_dir 下创建以 run_id 命名的子目录并写入 events.jsonl
# 设计：检查 tmp_path 下只有一个子目录且该目录包含 events.jsonl，确认目录结构约定（runs/<run_id>/events.jsonl）
async def test_run_creates_run_subdirectory(tmp_path: Path) -> None:
    await _run(tmp_path=tmp_path)
    subdirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(subdirs) == 1
    assert (subdirs[0] / "events.jsonl").exists()


# 功能：验证通过 extra_handlers 注入的回调能收到所有事件
# 设计：注入第二个收集器，确认 extra_handlers 机制有效；这是测试代码注入 mock 观察器、生产代码接入 StdoutPrinter 的同一扩展点
async def test_extra_handlers_receive_events(tmp_path: Path) -> None:
    secondary: list[BaseModel] = []

    async def _second(e: BaseModel) -> None:
        secondary.append(e)

    cfg = _config()
    runner = AgentRunner(
        cfg,
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[_second],
        runs_dir=tmp_path,
    )
    await runner.run("goal")
    assert len(secondary) > 0


# 功能：验证 config.agent.max_steps 被正确传递给 AgentLoop，控制 LLM 调用次数上限
# 设计：用 LoopingProvider 的调用次数反推 max_steps 是否生效，不依赖内部状态检查，从行为角度验证配置传递
async def test_config_max_steps_passed_to_loop(tmp_path: Path) -> None:
    provider = _LoopingProvider()
    await _run(provider=provider, config=_config(max_steps=3), tmp_path=tmp_path)
    assert provider._call == 3


# 功能：验证 run.started 和 run.finished 事件使用相同且非空的 run_id
# 设计：同时检查两个事件的 run_id 字段，确认 runner 在整个 run 生命周期使用同一个 run_id
async def test_run_id_embedded_in_started_event(tmp_path: Path) -> None:
    events = await _run(tmp_path=tmp_path)
    started = next(e for e in events if e.type == "run.started")  # type: ignore[attr-defined]
    finished = next(e for e in events if e.type == "run.finished")  # type: ignore[attr-defined]
    assert started.run_id == finished.run_id  # type: ignore[attr-defined]
    assert len(started.run_id) > 0  # type: ignore[attr-defined]


# 功能：验证注入外部 EventBus 时，runner 使用该 bus 而不自建，外部订阅者能收到所有事件
# 设计：显式传入 EventBus 实例并订阅收集器，确认 runner 不再内部新建 bus（否则外部订阅者收不到事件）；
#       这是 CoreApp 注入全局 bus 的核心行为，单元测试级别验证可避免集成测试的守护进程依赖
async def test_injected_bus_receives_events(tmp_path: Path) -> None:
    from kama_claude.core.events.bus import EventBus

    external_bus = EventBus()
    collected: list[object] = []

    async def collect(e: object) -> None:
        collected.append(e)

    external_bus.subscribe(collect)

    runner = AgentRunner(
        _config(),
        bus=external_bus,
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        runs_dir=tmp_path,
    )
    await runner.run("goal")

    types = [e.type for e in collected]  # type: ignore[attr-defined]
    assert "run.started" in types
    assert "run.finished" in types


# 功能：验证 session run 读取检查点工作历史，并把 Markdown 会话记忆注入 system prompt
# 设计：用 CapturingProvider 截获 LLM 入参，不触发真实 API；同时断言 run 目录写到 session/runs 下
async def test_session_history_and_memory_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from kama_claude.core.session.model import Session
    from kama_claude.core.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess-1",
        mode="chat",
        status="active",
        title="",
        created_at="t",
        updated_at="t",
    )
    store.write_meta(session)
    store.append_message("sess-1", "user", "remember python")
    MemoryStore(tmp_path).upsert(
        "python", summary="Python 3.12", content="Python 3.12", source="test",
    )

    provider = _CapturingProvider(LlmResponse(stop_reason="end_turn", text="done"))
    runner = AgentRunner(_config(), provider=provider, runs_dir=tmp_path / "runs")

    await runner.run_and_capture("remember python", run_id="run-new", session=session, store=store)

    assert provider.messages[-1:] == [{"role": "user", "content": "remember python"}]
    assert "Python 3.12" in str(provider.messages[0])
    assert provider.system is not None
    assert "Python 3.12" not in provider.system
    assert (store.runs_dir("sess-1") / "run-new" / "events.jsonl").exists()
    assert not (tmp_path / "runs" / "run-new").exists()


# 功能：验证 session run 注册 memory_write，工具调用写入会话 Markdown
# 设计：mock provider 第一步请求 memory_write、第二步 end_turn，覆盖 runner→registry→tool invocation 的完整路径
async def test_session_registers_memory_write_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from kama_claude.core.session.model import Session
    from kama_claude.core.session.store import SessionStore

    class _MemoryProvider:
        # 初始化调用计数器，用于返回两步响应
        def __init__(self) -> None:
            self.calls = 0

        # 第一步请求 memory_write，第二步返回 end_turn
        async def chat(
            self,
            messages: list[dict[str, object]],
            tool_schemas: list[dict[str, object]],
            bus: EventBus,
            run_id: str,
            *,
            step: int = 0,
            system: str | None = None,
            max_output_tokens: int = 8_192,
        ) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(
                            id="note-1",
                            name="memory_write",
                            input={"scope": "project", "key": "python", "summary": "Python",
                                   "content": "Use Python 3.12"},
                        )
                    ],
                )
            return LlmResponse(stop_reason="end_turn", text="noted")

    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess-1",
        mode="chat",
        status="active",
        title="",
        created_at="t",
        updated_at="t",
    )
    store.append_message("sess-1", "user", "remember")

    runner = AgentRunner(_config(max_steps=3), provider=_MemoryProvider(), runs_dir=tmp_path)
    await runner.run_and_capture("remember", run_id="run-1", session=session, store=store)

    assert MemoryStore(tmp_path).get(
        "python",
    ).content == "Use Python 3.12"


# 功能：验证模型客户端初始化退出时记录中断，并释放本轮订阅和事件写入器
# 设计：用抛 SystemExit 的本地工厂模拟缺少密钥，结束后再发总线事件检查观察者不会泄漏
async def test_provider_setup_failure_commits_interruption_and_cleans_observers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-setup", "chat", "active", "", "t", "t")
    store.write_meta(session)
    bus = EventBus()
    received: list[BaseModel] = []
    permanent: list[BaseModel] = []

    # 收集仅应在本轮有效的额外观察事件
    async def collect(event: BaseModel) -> None:
        received.append(event)

    # 收集生命周期长于本轮的总线事件
    async def collect_permanent(event: BaseModel) -> None:
        permanent.append(event)

    # 模拟默认模型客户端发现缺少凭证并要求退出
    def unavailable_provider(model: str) -> None:
        raise SystemExit("missing API key")

    monkeypatch.setattr("kama_claude.core.llm.factory.DeepseekProvider", unavailable_provider)
    bus.subscribe(collect_permanent)
    runner = AgentRunner(_config(), bus=bus, extra_handlers=[collect])
    outcome = await runner.run_and_capture(
        "goal", run_id="run-setup", session=session, store=store,
    )
    cp = store.checkpoints.load(session.id)
    assert outcome.status == "interrupted" and cp.run.status == "interrupted"
    assert cp.run.reason == "run_error" and cp.session["status"] == "interrupted"
    assert received[-1].type == "run.interrupted"
    assert not any(event.type == "run.finished" for event in received)
    event_path = store.runs_dir(session.id) / "run-setup" / "events.jsonl"
    saved_events = event_path.read_text(encoding="utf-8")
    before = len(received)
    permanent_before = len(permanent)
    await bus.publish_safe(RunStartedEvent(run_id="after", goal="probe", ts="t"))
    assert len(received) == before and len(permanent) == permanent_before + 1
    assert event_path.read_text(encoding="utf-8") == saved_events


# 功能：验证会话执行被取消后先提交中断并发出通知，再继续向上传播取消
# 设计：假模型在调用时取消，检查磁盘、历史文件和总线终态，避免拆分收尾后吞掉取消信号
async def test_cancellation_commits_interruption_before_reraising(tmp_path: Path) -> None:
    class CancellingProvider:
        # 在模型调用处模拟 Core 正常关闭导致的取消
        async def chat(self, **kwargs: object) -> LlmResponse:
            raise asyncio.CancelledError()

    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-cancel", "chat", "active", "", "t", "t")
    store.write_meta(session)
    received: list[BaseModel] = []

    # 收集收尾时发布的中断事件
    async def collect(event: BaseModel) -> None:
        received.append(event)

    runner = AgentRunner(
        _config(), provider=CancellingProvider(), extra_handlers=[collect],
    )
    with pytest.raises(asyncio.CancelledError):
        await runner.run_and_capture(
            "goal", run_id="run-cancel", session=session, store=store,
        )
    cp = store.checkpoints.load(session.id)
    assert cp.run.status == "interrupted" and cp.run.reason == "core_stopped"
    assert cp.messages[0] == {"role": "user", "content": "goal"}
    assert (store.session_dir(session.id) / "thread.jsonl").exists()
    assert received[-1].type == "run.interrupted"
