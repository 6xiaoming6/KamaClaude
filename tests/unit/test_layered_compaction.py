from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.compact import budget, pruning
from kama_claude.core.compact.budget import (
    ARCHIVED_RESULT_MARKER,
    estimate_request,
    estimate_tokens,
    read_archived_output,
    truncate_tool_results,
)
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.pruning import deduplicate_results, prune_results
from kama_claude.core.config import CompactionConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from tests.unit.test_compactor import history, provider
from tests.unit.test_s9_context import make_loop


# 构造完整工具批次，保留用户约束、模型决定及 thinking 以检查保护范围
def tool_history(
    count: int = 8, *, repeated: bool = True, name: str = "read_file", size: int = 1_600,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Only inspect files. Do not modify or commit."},
    ]
    for index in range(count):
        messages.extend([
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": f"opaque-{index}", "signature": "signature"},
                {"type": "text", "text": f"decision {index} must remain"},
                {"type": "tool_use", "id": f"call-{index}", "name": name, "input": {"path": "same"}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": f"call-{index}",
                "content": ("report" if repeated else f"report-{index}") + "中" * size,
            }]},
        ])
    return messages


# 返回可实际触发压力处理的小窗口，目标比例始终低于软阈值
def config() -> CompactionConfig:
    return CompactionConfig(
        context_window_tokens=8_192, output_reserve_tokens=512,
        summary_max_tokens=256, auto_threshold=0.8, prune_target_ratio=0.65,
    )


# 只提取模型可见的工具结果，便于检查 ID 与正文
def results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        block for message in messages if message["role"] == "user"
        and isinstance(message["content"], list)
        for block in message["content"] if block.get("type") == "tool_result"
    ]


# 功能：验证第一层预览足够时，不进入后续阶段或调用摘要
# 设计：构造原始请求超软阈值、头尾预览后低于阈值的大量 ASCII 输出
async def test_preview_layer_alone_avoids_llm(tmp_path: Path) -> None:
    messages = tool_history(10, size=20_000)
    for block in results(messages):
        block["content"] = "x" * 20_000
    model = provider()
    result = await Compactor().prepare_context(messages, model, outputs_dir=tmp_path)
    assert result.compacted and result.applied_stages == ["tool_preview"]
    assert not result.llm_used and result.reason == "below_threshold"
    assert len(list(tmp_path.glob("*.txt"))) == 10
    model.chat.assert_not_called()


@pytest.mark.parametrize(("repeated", "name", "stage"), [
    (True, "read_file", "deduplicate"), (False, "bash", "tool_archive"),
])
# 功能：验证第二层去重或第三层遮蔽分别能够单独解决压力，不额外调用模型
# 设计：重复只读输出只需去重，唯一命令输出必须走归档；对照元数据保护和输入不变性
async def test_program_layers_avoid_llm_and_preserve_structure(
    tmp_path: Path, repeated: bool, name: str, stage: str,
) -> None:
    messages = tool_history(repeated=repeated, name=name)
    original = copy.deepcopy(messages)
    model = provider()
    cfg = config()
    result = await Compactor(cfg).prepare_context(messages, model, outputs_dir=tmp_path)
    assert result.compacted and result.applied_stages == [stage]
    assert result.reason == "below_threshold" and not result.llm_used
    assert estimate_request("", result.messages, []) < cfg.input_limit * cfg.auto_threshold
    assert messages == original
    assert len(result.messages) == len(messages)
    assert result.messages[0] == original[0]
    for i, message in enumerate(original):
        if message["role"] == "assistant":
            assert result.messages[i] == message
    new_results = results(result.messages)
    assert new_results[-2:] == results(original)[-2:]
    assert all(set(block) == set(results(original)[i]) for i, block in enumerate(new_results))
    for block in new_results:
        if block["content"].startswith(ARCHIVED_RESULT_MARKER):
            full = read_archived_output(block["content"], block["tool_use_id"])
            assert full is not None and full[0] == results(original)[new_results.index(block)]["content"]
    model.chat.assert_not_called()


# 功能：验证没有工具输出可处理时，最终只调用一次摘要模型
# 设计：使用纯对话长历史，让前三层都无法缩减，同时保留可容纳的近期消息
async def test_llm_is_only_called_as_last_layer(tmp_path: Path) -> None:
    model = provider()
    result = await Compactor(config()).prepare_context(history(10), model, outputs_dir=tmp_path)
    assert result.compacted and result.llm_used
    assert result.applied_stages == ["llm_summary"]
    assert model.chat.call_count == 1


@pytest.mark.parametrize("difference", ["input", "output", "name", "error"])
# 功能：验证同路径不足以认定重复，参数、完整内容、工具名和错误状态都参与限制
# 设计：单独调用去重层以排除后续遮蔽层干扰，目标设为零检查所有可处理候选
def test_deduplication_requires_exact_match(tmp_path: Path, difference: str) -> None:
    messages = tool_history(2)
    if difference == "input":
        messages[3]["content"][-1]["input"]["path"] = "different"
    elif difference == "output":
        messages[4]["content"][0]["content"] += "changed"
    elif difference == "name":
        messages[3]["content"][-1]["name"] = "list_dir"
    else:
        messages[2]["content"][0]["is_error"] = True
    original = copy.deepcopy(messages)
    assert deduplicate_results(messages, tmp_path, len(messages), "", [], 0) == 0
    assert messages == original


# 功能：验证参数字典顺序不影响精确去重，工具调用 ID 重用也不会错误保护旧结果
# 设计：两个批次重用同一个 ID，参数只改变键顺序，比较归档短记录是否正确替代旧正文
def test_deduplication_normalizes_parameters_and_handles_reused_ids(tmp_path: Path) -> None:
    messages = tool_history(2)
    messages[1]["content"][-1]["input"] = {"path": "same", "depth": 1}
    messages[3]["content"][-1]["input"] = {"depth": 1, "path": "same"}
    messages[3]["content"][-1]["id"] = "call-0"
    messages[4]["content"][0]["tool_use_id"] = "call-0"
    assert deduplicate_results(messages, tmp_path, 3, "", [], 0) == 1
    assert results(messages)[0]["content"].startswith(ARCHIVED_RESULT_MARKER)
    assert not results(messages)[1]["content"].startswith(ARCHIVED_RESULT_MARKER)


# 功能：验证头尾相同而中间不同的输出不会被去重
# 设计：先用第一层生成相同头尾预览，再要求去重层读取归档中的真实中段
def test_identical_preview_does_not_mean_identical_output(tmp_path: Path) -> None:
    messages = tool_history(2)
    for i, block in enumerate(results(messages)):
        block["content"] = "A" * 2_000 + str(i) * 6_000 + "Z" * 2_000
    prepared = truncate_tool_results(messages, outputs_dir=tmp_path)
    text = [block["content"] for block in results(prepared)]
    assert text[0][:2_000] == text[1][:2_000] and text[0][-2_000:] == text[1][-2_000:]
    assert deduplicate_results(prepared, tmp_path, len(prepared), "", [], 0) == 0


# 功能：验证错误结果和过短结果不被去重或遮蔽，避免丢失诊断或负收益处理
# 设计：强制遍历旧结果，并比较完整输入，排除仅因低于软阈值跳过的情况
def test_errors_and_unprofitable_records_stay_untouched(tmp_path: Path) -> None:
    messages = tool_history(3)
    results(messages)[0]["is_error"] = True
    results(messages)[1]["content"] = "ok"
    results(messages)[2]["is_error"] = True
    original = copy.deepcopy(messages)
    assert deduplicate_results(messages, tmp_path, len(messages), "", [], 0) == 0
    assert prune_results(messages, tmp_path, len(messages), "", [], 0) == 0
    assert messages == original and not list(tmp_path.glob("*.txt"))


# 功能：验证未完成工具批次不进入去重、遮蔽或摘要阶段
# 设计：在高压力历史末尾放置无结果的工具调用，确认不是因空间充足而提前退出
async def test_pending_batch_blocks_pressure_layers(tmp_path: Path) -> None:
    messages = tool_history()
    messages.append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "pending", "name": "write_file", "input": {}},
    ]})
    model = provider()
    result = await Compactor(config()).prepare_context(messages, model, outputs_dir=tmp_path)
    assert result.reason == "incomplete_tool_batch" and result.messages == messages
    assert not result.applied_stages
    model.chat.assert_not_called()


# 功能：验证归档短记录可独立读取且多次处理不重复包装或覆盖原文
# 设计：第二次调用空间已足够，检查原文哈希、短记录和文件数都稳定
async def test_archive_records_are_independent_and_idempotent(tmp_path: Path) -> None:
    compactor = Compactor(config())
    first = await compactor.prepare_context(tool_history(), provider(), outputs_dir=tmp_path)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob("*.txt")}
    second = await compactor.prepare_context(first.messages, provider(), outputs_dir=tmp_path)
    assert not second.compacted and not second.llm_used
    assert second.messages == first.messages and not second.applied_stages
    assert files == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob("*.txt")}
    for block in results(second.messages):
        if block["content"].startswith(ARCHIVED_RESULT_MARKER):
            record = json.loads(block["content"][len(ARCHIVED_RESULT_MARKER):])
            assert Path(record["path"]).exists()
            assert read_archived_output(block["content"], block["tool_use_id"]) is not None


# 功能：验证自动禁用时仍保留第一层，手动操作可按百分之八十阈值处理压力
# 设计：相同高压力输入分别走自动和手动，确认手动程序层解决后也不使用模型
async def test_auto_disabled_does_not_disable_explicit_manual_pipeline(tmp_path: Path) -> None:
    cfg = config()
    cfg.auto_threshold = 0
    model = provider()
    automatic = await Compactor(cfg).prepare_context(
        tool_history(), model, outputs_dir=tmp_path / "auto",
    )
    assert automatic.reason == "auto_disabled" and not automatic.compacted
    manual = await Compactor(cfg).prepare_context(
        tool_history(), model, outputs_dir=tmp_path / "manual", manual=True,
    )
    assert manual.compacted and manual.applied_stages == ["deduplicate"]
    assert not manual.llm_used
    model.chat.assert_not_called()


# 功能：验证任意一层结束后低于软阈值即停止，不为达到目标比例额外删减
# 设计：按实际预览大小构造百分之七十二占用，位于目标与软阈值之间
async def test_layer_stops_below_soft_threshold_even_above_target(tmp_path: Path) -> None:
    messages = tool_history(6)
    for block in results(messages):
        block["content"] = "x" * 20_000
    prepared = truncate_tool_results(messages, outputs_dir=tmp_path)
    estimate = estimate_request("", prepared, [])
    cfg = config()
    cfg.context_window_tokens = int(estimate / 0.72) + cfg.output_reserve_tokens + 1_024
    model = provider()
    result = await Compactor(cfg).prepare_context(messages, model, outputs_dir=tmp_path)
    assert result.applied_stages == ["tool_preview"]
    assert cfg.input_limit * 0.65 < estimate_request("", result.messages, []) < cfg.input_limit * 0.8
    model.chat.assert_not_called()


# 功能：验证手动空间充足时无需初始化 SDK 或要求模型密钥
# 设计：模型工厂一旦被调用就抛 SystemExit，真实手动入口仍正常返回未调用模型
async def test_manual_under_budget_never_creates_provider(tmp_path: Path) -> None:
    factory = Mock(side_effect=SystemExit("missing API key"))
    store = SessionStore(tmp_path)
    manager = SessionManager(store, Mock(), EventBus(), provider_factory=factory)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "hello")
    revision = store.checkpoints.load(session.id).revision
    result = await manager.compact(session.id)
    assert result.reason == "below_threshold" and not result.compacted and not result.llm_used
    assert store.checkpoints.load(session.id).revision == revision
    factory.assert_not_called()


# 功能：验证程序缩减保存成功后，TUI 收到未调用模型的完成事件
# 设计：完成观察者直接读取权威检查点，确认候选和元数据都在保存后对外发布
async def test_manual_programmatic_result_is_saved_before_notification(tmp_path: Path) -> None:
    factory = Mock(side_effect=AssertionError("must not create a model"))
    store = SessionStore(tmp_path)
    bus = EventBus()
    manager = SessionManager(store, Mock(), bus, provider_factory=factory, compaction=config())
    session = await manager.create("chat")
    for message in tool_history():
        store.append_message(session.id, message["role"], message["content"])
    observed = []

    # 在真实完成事件中核对已提交内容，而不是仅检查 save 调用次数
    async def collect(event: Any) -> None:
        if event.type == "context.compacted":
            assert any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(store.read_messages(session.id)))
            observed.append(event)

    bus.subscribe(collect)
    response = await manager.compact(session.id)
    assert response.compacted and response.applied_stages == ["deduplicate"]
    assert not response.llm_used and len(observed) == 1 and not observed[0].llm_used
    assert response.model_dump()["applied_stages"] == ["deduplicate"]
    factory.assert_not_called()


# 功能：验证归档失败不提交候选、不调用模型，也不修改输入历史
# 设计：在实际压力去重归档时注入磁盘异常，覆盖长结果第一层以外的保存边界
async def test_pressure_archive_failure_does_not_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = tool_history()
    original = copy.deepcopy(messages)
    model = provider()
    monkeypatch.setattr(budget, "_archive_output", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        await Compactor(config()).prepare_context(messages, model, outputs_dir=tmp_path)
    assert messages == original
    model.chat.assert_not_called()


# 功能：验证程序层候选的检查点保存失败后，不能继续普通模型请求或发布成功
# 设计：输入可完全由去重解决，阻断保存后必须连摘要和普通模型调用都为零
async def test_programmatic_checkpoint_failure_stops_model(tmp_path: Path) -> None:
    model = provider()
    persistence = Mock()
    persistence.save.side_effect = CheckpointError("disk full")
    loop, _, events = make_loop(tmp_path, tool_history(), model, config(), persistence)
    with pytest.raises(CheckpointError):
        await loop.run()
    model.chat.assert_not_called()
    assert not any(event.type == "context.compacted" for event in events)


# 功能：验证摘要失败时已完成的程序缩减仍被保存，硬预算内继续普通请求
# 设计：大量 assistant 文字不能程序删除，先归档工具结果后仍触发一次摘要失败
async def test_summary_failure_keeps_programmatic_progress(tmp_path: Path) -> None:
    messages = [*history(16), *tool_history()]
    cfg = config()
    cfg.context_window_tokens = 18_432
    model = provider()
    model.chat.side_effect = [RuntimeError("summary offline"), LlmResponse("end_turn", text="done")]
    persistence = Mock()
    loop, context, events = make_loop(tmp_path, messages, model, cfg, persistence)
    await loop.run()
    assert context.run.status == "success" and model.chat.call_count == 2
    assert model.chat.call_args_list[0].kwargs["run_id"] == "compact"
    assert any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(context.messages))
    failed = next(event for event in events if event.type == "context.compaction_failed")
    assert failed.applied_stages and failed.llm_used and failed.reason == "summary_request_failed"
    assert persistence.save.called


# 功能：验证程序归档短记录在 Core 重新加载后仍可续跑，且跨新 Run 保留
# 设计：真实检查点提交后模拟硬崩溃，使用相同模型身份恢复，检查 Step 和持久化记录
async def test_programmatic_checkpoint_survives_reload_and_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_checkpoint_recovery import Crash, Provider, finish, manager

    store = SessionStore(tmp_path)
    model = Provider([LlmResponse("end_turn", text="done"), LlmResponse("end_turn", text="next")])
    mgr = manager(store, model, [])
    original_factory = mgr._runner_factory

    # 只给假模型配置小窗口，原 Runner 和恢复规则保持真实实现
    def factory():
        runner = original_factory()
        runner._config.compaction = config()
        return runner

    mgr._runner_factory = factory
    session = await mgr.create("chat")
    for message in tool_history():
        store.append_message(session.id, message["role"], message["content"])
    original_save = store.checkpoints.save

    # 在程序缩减提交后停止，外层 Runner 来不及收尾
    def crash_after_programmatic_save(cp: Any) -> None:
        original_save(cp)
        if any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(cp.messages)):
            raise Crash()

    monkeypatch.setattr(store.checkpoints, "save", crash_after_programmatic_save)
    with pytest.raises(Crash):
        await mgr.send_message(session.id, "continue")
    assert not model.calls
    saved = store.checkpoints.load(session.id)
    assert saved.schema_version == 1 and saved.run.step == 1
    monkeypatch.setattr(store.checkpoints, "save", original_save)
    restored = manager(store, model, [])
    restored._runner_factory = factory
    state = await restored.open(session.id)
    await restored.resume(session.id, state["revision"])
    await finish(restored, session.id)
    assert model.calls[-1]["step"] == 1
    assert any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(model.calls[-1]["messages"]))
    await restored.send_message(session.id, "followup")
    assert any(b["content"].startswith(ARCHIVED_RESULT_MARKER) for b in results(store.read_messages(session.id)))


# 功能：验证 TUI 区分程序完成和摘要完成，并如实说明部分程序结果已保存
# 设计：经真实会话过滤入口构造三个事件，检查界面文案不再误称全部原历史保留
def test_tui_displays_programmatic_and_partial_results(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.tui.app import KamaTuiApp

    app = KamaTuiApp("127.0.0.1", 1)
    app._session_id = "sess-layered"
    text = []
    monkeypatch.setattr(app._view, "append", lambda widget: text.append(str(widget.content)))
    base = {"session_id": "sess-layered", "run_id": "run", "original_tokens": 10_000,
            "summary_tokens": 0, "remaining_tokens": 3_000}
    app._handle_event({"type": "context.compacted", **base, "applied_stages": ["deduplicate"], "llm_used": False})
    app._handle_event({"type": "context.compacted", **base, "applied_stages": ["llm_summary"], "llm_used": True})
    app._handle_event({"type": "context.compaction_failed", **base, "reason": "summary_request_failed",
                       "applied_stages": ["tool_archive"], "llm_used": True})
    assert "未调用 LLM" in text[0] and "重复结果去重" in text[0]
    assert "已生成 LLM 摘要" in text[1]
    assert "程序化缩减已保存" in text[2] and "原历史保留" not in text[2]
    app._handle_event({"type": "context.compacted", **base, "session_id": "other"})
    assert len(text) == 3


# 功能：验证同一长任务在旧入口需要摘要，而分层入口可用程序处理解决
# 设计：复用已归档的相同长输出，统计真实摘要调用、逐层估算量及已验证归档的读取次数
async def test_comparison_reports_calls_tokens_and_archive_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    import re

    cfg = CompactionConfig(
        context_window_tokens=32_768, output_reserve_tokens=1_024, summary_max_tokens=512,
    )
    messages = tool_history(26)
    for block in results(messages):
        block["content"] = "HEAD" + "x" * 20_000 + "TAIL"
    prepared = truncate_tool_results(messages, outputs_dir=tmp_path / "outputs")
    reads = {"baseline": 0, "layered": 0}
    phase = "baseline"
    original_read = budget.read_archived_output

    # 只统计确实返回归档全文的读取，不把原始字符串检查算作文件读取
    def counted_read(text: str, tool_id: str):
        record = original_read(text, tool_id)
        if record is not None:
            reads[phase] += 1
        return record

    monkeypatch.setattr(budget, "read_archived_output", counted_read)
    monkeypatch.setattr(pruning, "read_archived_output", counted_read)
    baseline_model = provider()
    baseline = await Compactor(cfg).compact_messages(prepared, baseline_model)
    assert baseline.compacted and baseline_model.chat.call_count == 1
    phase = "layered"
    caplog.set_level(logging.INFO, logger="kama_claude.core.compact.compactor")
    layered_model = provider()
    layered = await Compactor(cfg).prepare_context(
        prepared, layered_model, outputs_dir=tmp_path / "outputs",
    )
    assert layered.compacted and not layered.llm_used
    layered_model.chat.assert_not_called()
    stage_counts = {}
    for record in caplog.records:
        match = re.search(r"stage=(\w+) input_before=(\d+) input_after=(\d+)", record.getMessage())
        if match:
            stage_counts[match[1]] = {"before": int(match[2]), "after": int(match[3])}
    assert "deduplicate" in stage_counts and reads["layered"] > 0
    report = {
        "scenario": "26 identical archived read_file results; fake summary model",
        "summary_calls": {"baseline": baseline_model.chat.call_count, "layered": layered_model.chat.call_count},
        "history_tokens": {"before": estimate_tokens(prepared),
                           "baseline_after": baseline.remaining_token_estimate,
                           "layered_after": layered.remaining_token_estimate},
        "stages": stage_counts, "archive_reads": reads,
    }
    print("LAYERED_COMPARISON=" + json.dumps(report, ensure_ascii=False))


# 功能：验证新目标比例的配置优先级及与软阈值的关系
# 设计：隔离项目和环境，仅用本地 TOML 与显式环境变量检验有效及无效组合
def test_prune_target_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from kama_claude.core.config import get_config

    for name in list(os.environ):
        if name.startswith("KAMA_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr("kama_claude.core.config.loader.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[compaction]\nprune_target_ratio=0.55\n", encoding="utf-8")
    monkeypatch.setenv("KAMA_CONFIG", str(config_path))
    assert get_config().compaction.prune_target_ratio == 0.55
    monkeypatch.setenv("KAMA_COMPACT_PRUNE_TARGET", "0.6")
    assert get_config().compaction.prune_target_ratio == 0.6
    monkeypatch.setenv("KAMA_COMPACT_THRESHOLD", "0.4")
    with pytest.raises(SystemExit, match="prune_target_ratio"):
        get_config()
    monkeypatch.setenv("KAMA_COMPACT_PRUNE_TARGET", "0.3")
    assert get_config().compaction.prune_target_ratio == 0.3
    monkeypatch.setenv("KAMA_COMPACT_THRESHOLD", "0")
    assert get_config().compaction.prune_target_ratio == 0.3
    monkeypatch.setenv("KAMA_COMPACT_PRUNE_TARGET", "nan")
    with pytest.raises(SystemExit, match="prune_target_ratio"):
        get_config()
