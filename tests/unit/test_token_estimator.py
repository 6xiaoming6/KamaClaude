from __future__ import annotations

import copy
import json

import pytest

from kama_claude.core.compact.estimator import (
    TokenCounter,
    TokenEstimator,
    UsageAnchor,
    estimate_bytes,
)
from kama_claude.core.config import KamaConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from tests.unit.loop_support import make_loop as build_test_loop


# 功能：验证 UTF-8 字节估算、向上取整和完整请求的结构字段
# 设计：分别计算中文、ASCII、emoji 的真实字节长度，不依赖分词器
def test_utf8_estimate_and_full_request():
    for text in ("", "a", "abcde", "中文", "😀"):
        assert estimate_bytes(text) == (len(text.encode("utf-8")) + 3) // 4
    counter = TokenCounter()
    messages = [{"role": "user", "content": "中文请求"}]
    assert counter.request("system", messages, [{"name": "read_file"}]) > counter.count(messages)


# 功能：验证上次总输入 Usage 加新增消息，旧回复不会漏算或重复计入
# 设计：锚点建立在请求输入末尾，随后追加 assistant 与配对工具结果
def test_usage_anchor_adds_only_tail():
    estimator = TokenEstimator("deepseek", "m")
    original = [{"role": "user", "content": "你好"}]
    estimator.observe("system", original, [], 300)
    messages = [*original,
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "read_file", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "中文结果"}]}]
    counter = estimator.select("system", messages, [])
    assert counter.method == "usage_anchor"
    assert counter.request("system", original, []) == 300
    assert counter.request("system", messages, []) == 300 + estimate_bytes(messages[1:])


@pytest.mark.parametrize("change", ["rewrite", "delete", "system", "tools", "model", "provider"])
# 功能：验证历史替换、系统提示、工具定义和模型变更使锚点失效
# 设计：旧消息数量不变也修改正文，排除仅靠长度判断前缀的错误实现
def test_changed_prefix_falls_back(change):
    estimator = TokenEstimator("deepseek", "m")
    messages = [{"role": "user", "content": "old"}]
    estimator.observe("system", messages, [], 1000)
    changed = copy.deepcopy(messages)
    system, tools = "system", []
    if change == "rewrite":
        changed[0]["content"] = "new"
    elif change == "delete":
        changed = []
    elif change == "system":
        system = "changed"
    elif change == "tools":
        tools = [{"name": "different"}]
    else:
        estimator = TokenEstimator("other" if change == "provider" else "deepseek",
                                   "other" if change == "model" else "m", estimator.anchors)
    counter = estimator.select(system, changed, tools)
    assert counter.method == "bytes"
    assert counter.underestimate_factor == 1
    assert counter.request(system, changed, tools) == counter.raw_request(system, changed, tools)


# 功能：验证 Cline 的分母是本次全文粗估，系数限制在一到四且只缩小预算
# 设计：让历史增长后粗估翻倍，检查阈值随当前请求变化而不是使用固定样本中位数
def test_dynamic_threshold_uses_current_full_estimate():
    estimator = TokenEstimator("deepseek", "m")
    messages = [{"role": "user", "content": "x" * 400}]
    raw = TokenCounter().raw_request("", messages, [])
    estimator.observe("", messages, [], raw * 2)
    counter = estimator.select("", messages, [])
    assert counter.underestimate_factor == 2
    assert counter.trigger(10000, 0.8) == 4000
    assert counter.effective_limit(10000) == 5000
    expanded = [*messages, {"role": "assistant", "content": "x" * 1000}]
    assert estimator.select("", expanded, []).underestimate_factor == 1
    estimator.observe("", messages, [], raw * 10)
    assert estimator.select("", messages, []).underestimate_factor == 4


# 功能：验证普通与摘要锚点隔离，持久化后仍只保留最新一轮有效 Usage
# 设计：使用 Pydantic JSON 往返模拟检查点加载，再发第二条观测覆盖第一条
def test_anchor_restore_latest_only_and_summary_isolation():
    estimator = TokenEstimator("deepseek", "m")
    messages = [{"role": "user", "content": "question"}]
    estimator.observe("", messages, [], 100)
    estimator.observe("", messages, [], 200)
    raw = json.loads(json.dumps({key: value.model_dump() for key, value in estimator.anchors.items()}))
    restored = TokenEstimator("deepseek", "m", {key: UsageAnchor.model_validate(value) for key, value in raw.items()})
    assert len(restored.anchors) == 1
    assert restored.select("", messages, []).request("", messages, []) == 200
    assert restored.select("", messages, [], kind="summary").method == "bytes"


@pytest.mark.parametrize("actual", [None, 0, -1, float("nan"), float("inf"), True])
# 功能：验证缺失或非法 Usage 不覆盖上次有效锚点
# 设计：保留旧输入计数作为观测基准，避免失败后错误重置为零
def test_invalid_usage_does_not_replace_anchor(actual):
    estimator = TokenEstimator()
    messages = [{"role": "user", "content": "question"}]
    estimator.observe("", messages, [], 100)
    estimator.observe("", messages, [], actual)
    assert estimator.select("", messages, []).request("", messages, []) == 100


# 功能：验证同轮候选比较不因锚点存在而伪造摘要收益，原前缀改变后全文重算
# 设计：令 API 用量远大于字节估算，对照 count 的同一口径与 request 的失效回退
def test_compaction_counter_keeps_raw_comparison():
    estimator = TokenEstimator()
    original = [{"role": "user", "content": "x" * 400}]
    estimator.observe("", original, [], 1000)
    counter = estimator.select("", original, [])
    candidate = [{"role": "user", "content": "y" * 400}]
    assert counter.count(candidate) == counter.count(original)
    assert counter.request("", candidate, []) == counter.raw_request("", candidate, [])


# 功能：验证运行保护配置保留，同时移除 tokenizer 配置入口
# 设计：默认额度关闭，环境覆盖生效，旧 tokenizer 小节按未知配置拒绝
def test_budget_config_and_removed_tokenizer_config(monkeypatch):
    config = KamaConfig()
    assert config.agent.max_total_tokens == config.agent.max_run_seconds == 0
    apply_toml(config, {"agent": {"max_run_seconds": 60, "repeat_failure_limit": 0}})
    monkeypatch.setenv("KAMA_AGENT_MAX_TOTAL_TOKENS", "20000")
    apply_env(config)
    assert config.agent.max_run_seconds == 60 and config.agent.max_total_tokens == 20000
    with pytest.raises(SystemExit):
        apply_toml(config, {"agent": {"max_run_seconds": -1}})
    with pytest.raises(SystemExit, match="Unknown top-level"):
        apply_toml(config, {"token_estimation": {"tokenizer_path": "unused"}})


# 功能：验证 Usage 锚点与动态阈值能触发单靠全文粗估不会触发的四层压缩
# 设计：模拟上一轮服务端输入比全文粗估多一倍，断言最终仅调用一次摘要
async def test_anchor_pressure_reaches_existing_compaction_pipeline():
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.config import CompactionConfig
    from tests.unit.test_compactor import history, provider
    messages = history()
    estimator = TokenEstimator()
    raw = TokenCounter().raw_request("", messages, [])
    estimator.observe("", messages, [], raw * 2)
    config = CompactionConfig(context_window_tokens=12000, output_reserve_tokens=512)
    assert raw < config.input_limit * config.auto_threshold
    model = provider()
    result = await Compactor(config, estimator).prepare_context(messages, model)
    assert result.compacted and result.llm_used
    assert model.chat.await_count == 1
    assert estimator.select("", result.messages, []).method == "bytes"


# 功能：验证多次工具结果追加到同一条消息时也不会漏算，改动旧锚点内的块则回退
# 设计：先给锚点外的 user 消息追加第二个结果，再修改锚点内的块
def test_tool_results_appended_inside_message_are_counted():
    estimator = TokenEstimator()
    old = [{"role": "user", "content": [{"type": "text", "text": "goal"}]}]
    estimator.observe("", old, [], 400)
    messages = copy.deepcopy(old)
    messages.append({"role": "user", "content": [{"type": "tool_result", "content": "one"}]})
    first = estimator.select("", messages, []).request("", messages, [])
    messages[-1]["content"].append({"type": "tool_result", "content": "two" * 50})
    assert estimator.select("", messages, []).request("", messages, []) > first
    messages[0]["content"].append({"type": "text", "text": "changed"})
    assert estimator.select("", messages, []).method == "bytes"


# 功能：验证受管模型真实采用锚点输入估算，Usage 缺失时不会移动边界
# 设计：第一次返回有效用量，第二次只返回文字，比较保存的预留和原锚点
async def test_managed_request_uses_anchor_and_keeps_it_without_usage():
    from kama_claude.core.config import CompactionConfig
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.managed import ManagedProvider
    from kama_claude.core.llm.types import LlmResponse, UsageStats
    from kama_claude.core.run_budget import RunBudget
    from tests.unit.test_run_protection import provider, run_state
    estimator = TokenEstimator()
    run = run_state()
    model = provider(LlmResponse("end_turn", text="one", usage=UsageStats(1000, 20)),
                     LlmResponse("end_turn", text="two"))
    managed = ManagedProvider(model, estimator, CompactionConfig(), budget=RunBudget(run))
    messages = [{"role": "user", "content": "goal"}]
    await managed.chat(messages, [], EventBus(), run.run_id, max_output_tokens=100)
    saved = copy.deepcopy(estimator.anchors)
    tail = [{"role": "assistant", "content": "one"}, {"role": "user", "content": "next"}]
    await managed.chat([*messages, *tail], [], EventBus(), run.run_id, max_output_tokens=100)
    assert run.estimated_tokens == 1000 + estimate_bytes(tail) + 100
    assert estimator.anchors == saved


# 功能：验证关闭自动压缩后仍按锚点估算检查硬预算，超限不请求模型
# 设计：给真实输入超限的锚点搭配很短消息，防止误用全文粗估放行
async def test_anchor_hard_limit_still_checked_when_auto_disabled():
    from kama_claude.core.config import CompactionConfig
    from kama_claude.core.context import ExecutionContext
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.tools.registry import ToolRegistry
    from tests.unit.test_run_protection import provider, run_state
    messages = [{"role": "user", "content": "goal"}]
    run = run_state(system_prompt="system")
    estimator = TokenEstimator()
    estimator.observe("system", messages, [], 40000)
    model = provider()
    context = ExecutionContext(run=run, messages=messages)
    loop = build_test_loop(model, ToolRegistry(), EventBus(), context=context, estimator=estimator,
                     compaction=CompactionConfig(auto_threshold=0))
    await loop.run()
    assert run.status == "interrupted" and run.reason == "context_budget_exceeded"
    model.chat.assert_not_called()
