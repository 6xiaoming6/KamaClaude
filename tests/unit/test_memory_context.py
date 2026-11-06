from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.context import build_memory_context, initialize_prompt
from kama_claude.core.memory.model import MemoryError
from tests.unit.test_memory_store import memory, put


# 功能：默认注入四个入口和项目索引，不自动加载主题正文。
# 设计：规则、摘要、详情分别使用独有文本，避免模糊包含断言掩盖全文泄漏。
def test_four_sources_and_lazy_details(tmp_path: Path) -> None:
    store = memory(tmp_path)
    store.home.mkdir(parents=True)
    (store.home / "AGENTS.md").write_text("GLOBAL_USER_RULE", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("PROJECT_USER_RULE", encoding="utf-8")
    store.write_global("GLOBAL_AUTO_KNOWLEDGE", store.read_global()["expected_hash"])
    put(store, summary="TOPIC_SUMMARY", content="PRIVATE_TOPIC_DETAILS")
    snapshot = build_memory_context(store)
    assert all(text in snapshot.text for text in (
        "GLOBAL_AUTO_KNOWLEDGE", "TOPIC_SUMMARY",
    ))
    assert "PRIVATE_TOPIC_DETAILS" not in snapshot.text
    assert len(snapshot.sources) == 2 and snapshot.estimated_tokens == estimate_bytes(snapshot.text)
    run = RunCheckpoint(run_id="r", goal="g", model="fake", workspace=str(tmp_path), max_steps=10)
    initialize_prompt(run, store, "SKILL")
    assert run.system_prompt.startswith("SKILL")
    assert "GLOBAL_USER_RULE" in run.system_prompt and "PROJECT_USER_RULE" in run.system_prompt
    assert "GLOBAL_AUTO_KNOWLEDGE" not in run.system_prompt
    assert "PROJECT_USER_RULE" not in snapshot.text
    assert "PRIVATE_TOPIC_DETAILS" in store.read("testing")["content"]


# 功能：用户规则不受自动预算裁剪，重复构建不会追加旧注入片段。
# 设计：极小自动预算配合较长用户规则，比较完整字符串与重复次数。
def test_rules_are_not_truncated(tmp_path: Path) -> None:
    store = memory(tmp_path, config=MemoryConfig(global_auto_tokens=1, project_index_tokens=1))
    rule = "USER_CONSTRAINT " * 300
    (tmp_path / "AGENTS.md").write_text(rule, encoding="utf-8")
    snapshot = build_memory_context(store)
    run = RunCheckpoint(run_id="r", goal="g", model="fake", workspace=str(tmp_path), max_steps=10)
    initialize_prompt(run, store, "BASE")
    assert rule.rstrip() in run.system_prompt and rule.rstrip() not in snapshot.text
    saved = run.system_prompt
    initialize_prompt(run, store, "BASE")
    assert saved == run.system_prompt


# 功能：旧三层文件不再读取，缺失入口不创建规则文件。
# 设计：旧目录放入独有标记，要求新快照和新目录都保持干净。
def test_legacy_files_are_ignored(tmp_path: Path) -> None:
    store = memory(tmp_path)
    old = tmp_path / ".kama" / "memory"
    old.mkdir(parents=True)
    (old / "old.md").write_text("OLD_PROJECT_MEMORY", encoding="utf-8")
    assert build_memory_context(store).text == ""
    assert not store.home.exists() and not (tmp_path / "AGENTS.md").exists()


# 功能：损坏编码与超预算自动文件阻止加载，不静默忽略或裁剪。
# 设计：直接写坏文件绕过专用工具，验证加载边界自身也会检查。
def test_load_errors_are_explicit(tmp_path: Path) -> None:
    store = memory(tmp_path)
    store.home.mkdir(parents=True)
    auto = store.global_path()
    auto.write_text("x" * 3_000, encoding="utf-8")
    with pytest.raises(MemoryError, match="shorten"):
        build_memory_context(store)
    auto.unlink()
    (store.home / "AGENTS.md").write_bytes(b"\xff")
    with pytest.raises(MemoryError, match="cannot read"):
        initialize_prompt(
            RunCheckpoint(run_id="r", goal="g", model="fake", workspace=str(tmp_path), max_steps=1),
            store, "BASE",
        )
