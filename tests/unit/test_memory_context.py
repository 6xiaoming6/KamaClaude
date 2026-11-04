from __future__ import annotations

from pathlib import Path

from kama_claude.core.compact.budget import estimate_tokens
from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.context import build_memory_context
from tests.unit.test_memory_store import memory, put


# 功能：验证固定优先、日期其次、相同日期按 key 排序，且不依赖目录枚举顺序
# 设计：手工设置相同时间并先写较旧固定条目，检查排序的三个条件
def test_deterministic_pinned_recent_key_order(tmp_path: Path) -> None:
    store = memory(tmp_path)
    for key in ("z", "a", "pinned"):
        put(store, key=key, pinned=key == "pinned")
        path = store.directory("project") / (key + ".md")
        entry = store.get("project", key)
        date = "2020-01-01T00:00:00+00:00" if key == "pinned" else "2026-01-01T00:00:00+00:00"
        path.write_text(path.read_text(encoding="utf-8").replace(entry.updated_at, date), encoding="utf-8")
    assert [entry.key for entry in store.scan("project")[0]] == ["pinned", "a", "z"]
    snapshot = build_memory_context(store, MemoryConfig(), include_session=True)
    assert snapshot.layers[1].included == ["pinned", "a", "z"]


# 功能：验证中文正文放不下时退到摘要，仍不足则记录省略而不突破预算
# 设计：混合超长正文、短摘要和多个条目，逐层检查估算量及原文件字节
def test_bounded_full_summary_and_omission(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store, key="long", content="中" * 1_000, pinned=True)
    for index in range(10):
        put(store, key=f"other_{index}", content="其他内容" * 50)
    original = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    config = MemoryConfig(global_tokens=0, project_tokens=80, session_tokens=0)
    snapshot = build_memory_context(store, config, include_session=True)
    project = snapshot.layers[1]
    assert project.summarized[0] == "long" and project.omitted > 0
    assert project.estimated_tokens <= 80 and snapshot.estimated_tokens <= 80
    assert estimate_tokens(snapshot.text) == snapshot.estimated_tokens
    assert original == {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    assert "memory_read" in snapshot.text


# 功能：验证三层标题和索引也计预算，多个作用域组合不会额外超额
# 设计：为三层分别写中文条目，比较完整片段与各层实际估算
def test_three_layer_token_accounting(tmp_path: Path) -> None:
    store = memory(tmp_path)
    for scope in ("global", "project", "session"):
        put(store, scope, content="记忆正文" * 25)
    config = MemoryConfig(global_tokens=150, project_tokens=160, session_tokens=170)
    snapshot = build_memory_context(store, config, include_session=True)
    assert all(layer.estimated_tokens <= layer.budget for layer in snapshot.layers)
    assert snapshot.estimated_tokens <= sum(layer.budget for layer in snapshot.layers)
    assert [layer.scope for layer in snapshot.layers] == ["global", "project", "session"]


# 功能：验证关闭注入与极小预算都不会裁剪源文件或创建额外文件
# 设计：0 预算仍可通过存储读取，1 token 预算连标题放不下时返回明确省略数
def test_disabled_and_tiny_budgets(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    disabled = build_memory_context(
        store, MemoryConfig(global_tokens=0, project_tokens=0, session_tokens=0), include_session=True,
    )
    assert disabled.text == "" and disabled.estimated_tokens == 0
    tiny = build_memory_context(
        store, MemoryConfig(global_tokens=0, project_tokens=1, session_tokens=0), include_session=True,
    )
    assert tiny.text == "" and tiny.layers[1].omitted == 1
    assert store.get("project", "python").content == "Python 3.12"


# 功能：验证损坏来源在快照中明确可查，其他有效内容仍可注入
# 设计：有效条目旁放非法 Markdown，检查警告不会消失，也不触发自动修复
def test_corruption_is_visible_in_snapshot(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    path = store.directory("project") / "broken.md"
    path.write_text("broken", encoding="utf-8")
    snapshot = build_memory_context(store, MemoryConfig(), include_session=True)
    assert snapshot.layers[1].warnings and "Unreadable files" in snapshot.text
    assert "Python 3.12" in snapshot.text and path.read_text() == "broken"


# 功能：验证无会话的内存执行仅组织全局与项目层
# 设计：通过显式 include_session 关闭会话读取，不靠捕获不存在目录来伪装为空
def test_standalone_context_has_no_session_layer(tmp_path: Path) -> None:
    store = memory(tmp_path)
    snapshot = build_memory_context(store, MemoryConfig(), include_session=False)
    assert [layer.scope for layer in snapshot.layers] == ["global", "project"]
