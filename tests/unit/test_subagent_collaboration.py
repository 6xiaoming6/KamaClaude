from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.events import SubagentFinishedEvent
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint, ToolCheckpoint
from kama_claude.core.config import KamaConfig, SubagentConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.runner import AgentRunner
from kama_claude.core.subagent.query import AgentResultParams, AgentResultTool
from kama_claude.core.subagent.registry import ActiveSubagent, BackgroundTaskRegistry
from kama_claude.core.subagent.report import write_report
from kama_claude.core.subagent.result import PENDING_RESULT, SubagentResult
from kama_claude.core.subagent.tool import SpawnAgentTool
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import ToolResult
from tests.unit.completion_support import submit_response


class Model:
    # 用事件控制完成时机，测试不依赖付费模型和固定运行速度。
    def __init__(self, blocked: bool = False, text: str = "answer") -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.text = text
        self.calls: list[dict[str, Any]] = []
        if not blocked:
            self.release.set()

    # 记录实际请求以验证提示注入，不继承父对话。
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return submit_response(
            LlmResponse("end_turn", text=self.text, usage=UsageStats(10, 5)),
            kwargs["tool_schemas"], kwargs["run_id"], kwargs["step"],
        )


# 构造共享注册表的工具，允许分别改变根 Run 与会话身份。
def make_tool(
    path: Path, model: Model, registry: BackgroundTaskRegistry, *,
    root: str = "root", sid: str = "session", limits: SubagentConfig | None = None,
    bus: EventBus | None = None,
) -> SpawnAgentTool:
    return SpawnAgentTool(
        model, bus or EventBus(), root, None, 5, registry, path / "runs", sid,
        workspace=path, subagent_config=limits,
    )


# 取得工具返回的 Run ID，并保留完整结果供限制断言使用。
async def start(tool: SpawnAgentTool) -> tuple[str, ToolResult]:
    result = await tool.invoke({"description": "child", "prompt": "goal", "run_in_background": True})
    run_id = "" if result.is_error else result.content.split("run_id=")[1].split(".")[0]
    return run_id, result


# 等待注册表中已登记的 Task，避免通过轮询猜测完成时刻。
async def finish(registry: BackgroundTaskRegistry, run_id: str, sid: str = "session") -> SubagentResult:
    entry = registry.get(run_id, sid)
    if isinstance(entry, ActiveSubagent):
        await asyncio.gather(entry.task, return_exceptions=True)
    result = registry.get(run_id, sid)
    assert isinstance(result, SubagentResult)
    return result


# 功能：立即查询和等待超时不取消子任务，后续查询可以取得原任务结果。
# 设计：用阻塞模型强制超时，释放后检查同一个 Run ID 而非重新派生。
async def test_bounded_wait_preserves_child(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    run_id, _ = await start(make_tool(tmp_path, model, registry))
    query = AgentResultTool(registry, "session")
    await model.started.wait()
    for duration in (0, 0.01):
        result = await query.invoke({"run_ids": [run_id], "wait_s": duration})
        assert PENDING_RESULT in result.content
        entry = registry.get(run_id, "session")
        assert isinstance(entry, ActiveSubagent) and not entry.task.cancelled()
    model.release.set()
    result = await query.invoke({"run_ids": [run_id]})
    cached = await query.invoke({"run_ids": [run_id], "wait_s": 30})
    assert result == cached and "answer" in result.content and not result.is_error
    assert len(model.calls) == 1


# 功能：查询者取消继续抛出取消，子任务仍可完成。
# 设计：取消正在默认十秒等待的查询，再通过独立入口取得结果。
async def test_waiter_cancellation_is_isolated(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    run_id, _ = await start(make_tool(tmp_path, model, registry))
    await model.started.wait()
    waiter = asyncio.create_task(AgentResultTool(registry, "session").invoke({"run_ids": [run_id]}))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    entry = registry.get(run_id, "session")
    assert isinstance(entry, ActiveSubagent) and not entry.task.done()
    model.release.set()
    assert (await finish(registry, run_id)).status == "success"


# 功能：子任务自身取消转换为取消结果，不取消查询者。
# 设计：查询和子任务是两个独立 Task，只取消后者并检查已保存报告。
async def test_child_cancellation_returns_result(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    run_id, _ = await start(make_tool(tmp_path, model, registry))
    await model.started.wait()
    waiter = asyncio.create_task(AgentResultTool(registry, "session").invoke({"run_ids": [run_id]}))
    await asyncio.sleep(0)
    entry = registry.get(run_id, "session")
    assert isinstance(entry, ActiveSubagent)
    entry.task.cancel()
    result = await waiter
    assert result.is_error and "cancelled" in result.content
    cancelled = await finish(registry, run_id)
    assert Path(cancelled.report_path).exists()
    # 活跃名额释放，但已成功派生的累计数不会减少。
    assert registry._roots[("session", "root")].spawned == {run_id}


# 功能：等待参数拒绝越界与非有限数，默认十秒且支持立即查询。
# 设计：直接校验真实 Schema 对应的参数模型，避免等待实际超时时间。
@pytest.mark.parametrize("value", [-1, 31, float("nan"), float("inf"), "bad"])
def test_invalid_wait(value: Any) -> None:
    with pytest.raises(ValidationError):
        AgentResultParams.model_validate({"run_ids": ["child"], "wait_s": value})
    assert AgentResultParams(run_ids=["child"]).wait_s == 10


# 功能：并发派生不会超过活跃上限，拒绝的请求不产生开始事件或 Task。
# 设计：六个并发调用争抢三个名额，模型阻塞确保名额不会提前释放。
async def test_concurrent_reservation_is_bounded(tmp_path: Path) -> None:
    registry, model, bus = BackgroundTaskRegistry(), Model(blocked=True), EventBus()
    events: list[BaseModel] = []

    # 收集事件以区别真正派生和被限流的调用。
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    tool = make_tool(tmp_path, model, registry, bus=bus, limits=SubagentConfig(max_active=3))
    results = await asyncio.gather(*(start(tool) for _ in range(6)))
    accepted = [run_id for run_id, result in results if not result.is_error]
    assert len(accepted) == len(registry._tasks) == 3
    assert sum(result.error_type == "resource_limit" for _, result in results) == 3
    assert sum(getattr(event, "type", "") == "subagent.started" for event in events) == 3
    model.release.set()
    await asyncio.gather(*(finish(registry, run_id) for run_id in accepted))
    assert not registry._tasks


# 功能：前台、后台和孙任务共享根 Run 名额，达到上限立即失败而不等待。
# 设计：前台父子任务阻塞期间直接使用它装配的嵌套工具，强制占满单个名额。
async def test_foreground_and_nested_share_root(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(), Model(blocked=True)
    tool = make_tool(tmp_path, model, registry, limits=SubagentConfig(max_active=1))
    foreground = asyncio.create_task(tool.invoke({"description": "fg", "prompt": "goal"}))
    await model.started.wait()
    child_id = next(iter(registry._tasks))
    nested = tool._build_child_registry(EventBus(), child_id, None).get("spawn_agent")
    assert isinstance(nested, SpawnAgentTool) and nested._root_run_id == "root"
    _, denied = await start(nested)
    assert denied.error_type == "resource_limit" and len(registry._tasks) == 1
    model.release.set()
    assert not (await foreground).is_error
    _, accepted = await start(nested)
    assert not accepted.is_error
    await registry.cancel()


# 功能：实际嵌套工具调用沿用根身份，子孙任务都计入同一个累计额度。
# 设计：假模型通过真实 Loop 派生孙任务，避免只验证构造参数而遗漏调用链。
async def test_real_nested_execution_counts_descendants(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(), Model()
    roles = tmp_path / ".kama" / "agents"
    roles.mkdir(parents=True)
    (roles / "coordinator.toml").write_text(
        '[agent]\ndescription="Delegate a bounded task"\n'
        'allowed_tools=["spawn_agent", "agent_result", "agent_cancel", "read_file"]\n',
        encoding="utf-8",
    )
    seen: set[str] = set()

    # 父任务首次回复请求一个孙任务，其余调用立即结束。
    async def chat(**kwargs: Any) -> LlmResponse:
        run_id = kwargs["run_id"]
        if not seen:
            seen.add(run_id)
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock("spawn", "spawn_agent", {
                "description": "grandchild", "prompt": "grandchild goal",
            })], usage=UsageStats(10, 5))
        seen.add(run_id)
        return submit_response(
            LlmResponse("end_turn", text="done", usage=UsageStats(10, 5)),
            kwargs["tool_schemas"], run_id, kwargs["step"],
        )

    model.chat = chat  # type: ignore[method-assign]
    tool = make_tool(tmp_path, model, registry, limits=SubagentConfig(max_total=2))
    result = await tool.invoke({
        "description": "child", "prompt": "goal", "subagent_type": "coordinator",
    })
    assert not result.is_error and len(seen) == 2
    assert registry._roots[("session", "root")].spawned == seen
    assert len(list((tmp_path / "runs").glob("*/report.md"))) == 2
    _, denied = await start(tool)
    assert denied.error_type == "resource_limit" and not registry._tasks


# 功能：完成通知到达之前报告已写好，结果查询不会先看到成功却找不到全文。
# 设计：在实际完成事件的观察者中读取报告，而非在任务结束以后补查。
async def test_report_precedes_notification(tmp_path: Path) -> None:
    registry, model, bus = BackgroundTaskRegistry(), Model(), EventBus()
    reports: list[str] = []

    # 通知时同步读取报告，并检查已缓存的轻量结果。
    async def inspect(event: BaseModel) -> None:
        if isinstance(event, SubagentFinishedEvent):
            result = registry.get(event.run_id, "session")
            assert isinstance(result, SubagentResult)
            reports.append(Path(result.report_path).read_text(encoding="utf-8"))

    bus.subscribe(inspect)
    run_id, _ = await start(make_tool(tmp_path, model, registry, bus=bus))
    await finish(registry, run_id)
    assert len(reports) == 1 and "answer" in reports[0]


# 功能：查询隔离会话，缓存淘汰后从磁盘取回原结果，不重新派生。
# 设计：分别在活跃与淘汰后查询，核对调用次数和原任务状态。
async def test_live_session_isolation_and_expired_result(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(max_results=1), Model(blocked=True)
    tool = make_tool(tmp_path, model, registry)
    first, _ = await start(tool)
    denied = await AgentResultTool(registry, "other").invoke({"run_ids": [first]})
    assert denied.is_error
    assert isinstance(registry.get(first, "session"), ActiveSubagent)
    model.release.set()
    await finish(registry, first)
    second, _ = await start(tool)
    await finish(registry, second)
    expired = await AgentResultTool(registry, "session").invoke({"run_ids": [first]})
    assert not expired.is_error and first in expired.collected_subagents
    assert len(model.calls) == 2


# 功能：初始化失败和取消释放预留，不计入累计派生数。
# 设计：在 Task 建立前让装配函数失败，随后恢复装配验证同一配额仍可用。
@pytest.mark.parametrize("cancelled", [False, True])
async def test_initialization_releases_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    registry, model = BackgroundTaskRegistry(), Model()
    tool = make_tool(tmp_path, model, registry, limits=SubagentConfig(max_total=1))
    with monkeypatch.context() as patch:
        error = asyncio.CancelledError() if cancelled else RuntimeError("initialization failed")
        # 装配是同步入口，异常应发生在创建 Task 之前。
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise error

        patch.setattr("kama_claude.core.subagent.tool.create_agent_loop", fail)
        with pytest.raises(type(error)):
            await start(tool)
    quota = registry._roots[("session", "root")]
    assert not quota.spawned and not quota.pending and not registry._tasks
    run_id, result = await start(tool)
    assert not result.is_error
    await finish(registry, run_id)


# 功能：缓存淘汰、更换工具和 Runner 不重置根 Run 的累计额度。
# 设计：容量一的缓存淘汰首个结果，再构造新 Runner 补入原根身份。
async def test_total_survives_cache_and_runner_change(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(max_results=1), Model()
    limits = SubagentConfig(max_total=2)
    tool = make_tool(tmp_path, model, registry, limits=limits)
    ids: list[str] = []
    for _ in range(2):
        run_id, _ = await start(tool)
        ids.append(run_id)
        await finish(registry, run_id)
    assert ids[0] not in registry._results
    assert isinstance(registry.get(ids[0], "session"), SubagentResult)
    config = KamaConfig(subagent=limits)
    runner = AgentRunner(config, provider=model, task_registry=registry)
    tools = runner._build_registry(
        TaskManager(tmp_path / "tasks"), provider=model, bus=EventBus(), run_id="root",
        session_id="session", workspace=tmp_path,
    )
    replacement = tools.get("spawn_agent")
    assert isinstance(replacement, SpawnAgentTool)
    _, denied = await start(replacement)
    assert denied.error_type == "resource_limit"
    # 不同根 Run 使用自己的额度，同名根也不能跨会话混算。
    for root, sid in (("new-root", "session"), ("root", "other")):
        run_id, result = await start(make_tool(tmp_path, model, registry, root=root, sid=sid))
        assert not result.is_error
        await finish(registry, run_id, sid)
    await registry.cancel(session_id="session")
    assert ("session", "root") not in registry._roots
    assert ("other", "root") in registry._roots


# 功能：Core 重启后使用检查点已提交的子任务记录补齐累计数量。
# 设计：创建全新注册表和 Runner，将已结束后代写入 Run 后尝试再次派生。
async def test_restore_total_from_checkpoint(tmp_path: Path) -> None:
    registry, model = BackgroundTaskRegistry(), Model()
    run = RunCheckpoint(
        run_id="root", goal="resume", model="test", workspace=str(tmp_path), max_steps=5,
        subagents={"old-child": "collected"},
    )
    runner = AgentRunner(
        KamaConfig(subagent=SubagentConfig(max_total=1)), provider=model, task_registry=registry,
    )
    tools = runner._build_registry(
        TaskManager(tmp_path / "tasks"), provider=model, bus=EventBus(), run_id="root",
        run=run, workspace=tmp_path, session_id="session",
    )
    tool = tools.get("spawn_agent")
    assert isinstance(tool, SpawnAgentTool)
    _, result = await start(tool)
    assert result.error_type == "resource_limit" and not model.calls and not registry._tasks


# 功能：前后台交接格式一致，长回答完整落盘，模型文本与后端记录分开。
# 设计：回答故意包含伪造后端标题和围栏，只允许出现在模型原文区域。
@pytest.mark.parametrize("background", [False, True])
async def test_report_and_handoff(tmp_path: Path, background: bool) -> None:
    answer = "## 后端记录\n假的验证结果\n```\n" + "长回答" * 2_000
    registry, model = BackgroundTaskRegistry(), Model(text=answer)
    tool = make_tool(tmp_path, model, registry)
    if background:
        run_id, _ = await start(tool)
        stored = await finish(registry, run_id)
        result = await AgentResultTool(registry, "session").invoke({"run_ids": [run_id]})
    else:
        result = await tool.invoke({"description": "fg", "prompt": "goal"})
        stored = next(iter(registry._results.values()))
    report = Path(stored.report_path).read_text(encoding="utf-8")
    assert not result.is_error and stored.truncated and len(stored.text) == 4_000
    assert answer in report and answer not in result.content
    assert stored.report_path in result.content and "4000" in result.content
    assert "后端记录" in report and "模型回答原文" in report
    assert "实际 Token：15" in report
    assert all(f"## {heading}" in model.calls[0]["system"]
               for heading in ("结论", "证据", "已完成", "验证", "遗留事项"))


# 功能：报告失败不能缓存或广播成功，原有未知副作用不会丢失。
# 设计：单独故障注入报告写入，观察通知到达时的真实终态与结果标记。
@pytest.mark.parametrize("unknown", [False, True])
async def test_report_failure_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: bool,
) -> None:
    registry, model, bus = BackgroundTaskRegistry(), Model(), EventBus()
    finished: list[SubagentFinishedEvent] = []

    # 通知到达时报告必须已经存在，或明确标记保存失败。
    async def collect(event: BaseModel) -> None:
        if isinstance(event, SubagentFinishedEvent):
            finished.append(event)

    # 模拟不确定的工具副作用，不执行真实命令。
    async def run(loop: Any) -> None:
        context = loop._context
        context.run.result = "model says success"
        if unknown:
            context.run.tools = [ToolCheckpoint(id="t", name="bash", input={}, status="unknown")]
            context.mark_interrupted("unknown_tool_result")
        else:
            context.mark_success()

    # 文件系统写入失败应变成明确的报告失败结果。
    def fail(*args: Any) -> Path:
        raise OSError("disk full")

    bus.subscribe(collect)
    monkeypatch.setattr("kama_claude.core.subagent.tool.AgentLoop.run", run)
    monkeypatch.setattr("kama_claude.core.subagent.registry.write_report", fail)
    run_id, _ = await start(make_tool(tmp_path, model, registry, bus=bus))
    result = await finish(registry, run_id)
    assert result.reason == "report_save_failed" and not result.report_path
    assert result.as_tool_result().is_error and result.outcome_unknown == unknown
    assert len(finished) == 1 and finished[0].status != "success"


# 功能：执行证据只收集当前子 Run，保存失败不破坏旧报告或遗留临时文件。
# 设计：混入孙任务命令与模型伪造证据，直接核对报告事件来源和原子替换边界。
def test_report_evidence_and_atomic_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = []
    for run_id, command in (("child", "real-command"), ("grandchild", "nested-command")):
        events.extend([
            {"type": "tool.call_started", "run_id": run_id, "tool_name": "bash",
             "tool_use_id": "t", "params": {"command": command}},
            {"type": "tool.call_finished", "run_id": run_id, "tool_name": "bash",
             "tool_use_id": "t"},
        ])
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8",
    )
    run = RunCheckpoint(
        run_id="child", goal="goal", status="success", result="claimed evidence",
        model="test", workspace=str(tmp_path), max_steps=5,
    )
    path = write_report(run, tmp_path)
    original = path.read_text(encoding="utf-8")
    assert "real-command" in original and "nested-command" not in original
    assert original.index("claimed evidence") > original.index("模型回答原文")

    # 模拟最后一步原子替换失败，旧报告必须仍可读取。
    def fail(*args: Any) -> None:
        raise OSError("replace denied")

    monkeypatch.setattr("kama_claude.core.subagent.report.os.replace", fail)
    with pytest.raises(OSError):
        write_report(run, tmp_path)
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".report-*.tmp"))


# 功能：子任务数量限制默认启用，TOML 与环境覆盖顺序保持一致。
# 设计：仅调用现有小节解析函数，排除机器全局配置和 dotenv 的干扰。
def test_subagent_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = KamaConfig()
    assert (config.subagent.max_active, config.subagent.max_total) == (2, 6)
    apply_toml(config, {"subagent": {"max_active": 2, "max_total": 5}})
    monkeypatch.setenv("KAMA_SUBAGENT_MAX_ACTIVE", "4")
    monkeypatch.setenv("KAMA_SUBAGENT_MAX_TOTAL", "7")
    apply_env(config)
    assert (config.subagent.max_active, config.subagent.max_total) == (4, 7)


# 功能：数量限制拒绝零、负数、布尔值、未知字段及错误小节类型。
# 设计：覆盖配置入口的真实类型边界，避免布尔值被当成整数放行。
@pytest.mark.parametrize("value", [{"max_active": 0}, {"max_total": -1}, {"max_active": True},
                                  {"max_total": "3"}, {"typo": 1}, []])
def test_subagent_config_rejects_invalid(value: Any) -> None:
    with pytest.raises(SystemExit):
        apply_toml(KamaConfig(), {"subagent": value})


# 功能：环境变量同样拒绝无效数量，不允许静默恢复默认值。
# 设计：逐一覆盖零、负数与非整数字符串，确保配置错误在启动时暴露。
@pytest.mark.parametrize("value", ["0", "-2", "true", "1.5"])
def test_subagent_env_rejects_invalid(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAMA_SUBAGENT_MAX_TOTAL", value)
    with pytest.raises(SystemExit):
        apply_env(KamaConfig())
