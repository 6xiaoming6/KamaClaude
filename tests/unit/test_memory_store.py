from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.model import MemoryError
from kama_claude.core.memory.paths import project_id
from kama_claude.core.memory.store import MemoryStore


# 绑定临时用户目录，存储测试不会触碰真实用户资料。
def memory(path: Path, **kwargs) -> MemoryStore:
    return MemoryStore(path, home=path / "home" / ".kama", **kwargs)


# 默认创建带详细正文的项目记忆，方便区分索引和全文。
def put(store: MemoryStore, key: str = "testing", **kwargs):
    return store.upsert(
        key, summary=kwargs.pop("summary", "Use focused tests"),
        content=kwargs.pop("content", "Full details are loaded only on demand."),
        source="session / run", **kwargs,
    )


# 功能：验证主题 Markdown、分类索引及按需读取，不把全文混入索引。
# 设计：索引与正文使用不同文本，并通过真实落盘内容检查固定头部。
def test_markdown_and_index(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store, type="feedback")
    assert "- 类型：feedback" in store.path("testing").read_text(encoding="utf-8")
    assert "testing.md" in store.index() and "Full details" not in store.index()
    assert store.read("testing")["content"].startswith("Full details")
    assert store.list(query="details")["total"] == 1
    assert store.list(query="missing")["entries"] == []


# 功能：内容不变不改时间，重复删除安全，索引缺失与损坏可重建。
# 设计：比较实际文件字节，并手动破坏派生索引验证其非权威地位。
def test_idempotency_and_index_repair(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    original = store.path("testing").read_bytes()
    assert put(store)["status"] == "unchanged"
    assert store.path("testing").read_bytes() == original
    index = store.root / "MEMORY.md"
    index.write_text("stale", encoding="utf-8")
    assert "testing.md" in store.index() and index.read_text(encoding="utf-8") != "stale"
    index.unlink()
    assert "testing.md" in store.read("testing")["path"] or index.exists()
    assert store.delete("testing")["status"] == "deleted"
    assert store.delete("testing")["status"] == "already_absent"


# 功能：目录路径、保留名称和索引不能成为记忆 key。
# 设计：跨平台加入 Windows 保留文件名及不同大小写的索引名称。
@pytest.mark.parametrize("key", ["../x", "x/y", "x\\y", "MEMORY", "memory", "CON", "", "."])
def test_invalid_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(MemoryError, match="invalid memory key"):
        put(memory(tmp_path), key)


# 功能：索引预算不足时拒绝新内容，正文和索引都不部分提交。
# 设计：很小的合法预算配合长摘要，在真正原子写入之前就触发错误。
def test_index_budget_before_write(tmp_path: Path) -> None:
    store = memory(tmp_path, config=MemoryConfig(project_index_tokens=5))
    with pytest.raises(MemoryError, match="shorten"):
        put(store)
    assert not store.root.exists()


# 功能：全局自动文件更新使用 hash，旧 hash 不会覆盖新内容。
# 设计：两位写入者共用旧 hash，第二次写入必须产生 edit_conflict。
def test_global_hash_and_budget(tmp_path: Path) -> None:
    store = memory(tmp_path)
    original = store.read_global()
    assert original["expected_hash"] == hashlib.sha256(b"").hexdigest()
    store.write_global("Use concise explanations.", original["expected_hash"])
    with pytest.raises(MemoryError) as error:
        store.write_global("stale update", original["expected_hash"])
    assert error.value.error_type == "edit_conflict"
    current = store.read_global()
    with pytest.raises(MemoryError, match="shorten"):
        store.write_global("x" * 3_000, current["expected_hash"])
    assert store.read_global() == current
    assert store.write_global(current["content"], current["expected_hash"])["status"] == "unchanged"
    assert store.write_global(None, current["expected_hash"])["status"] == "deleted"
    assert store.write_global(None, current["expected_hash"])["status"] == "already_absent"


# 功能：原子替换失败保留旧正文，并清理临时文件。
# 设计：在实际 replace 边界故障注入，而非仅模拟工具返回错误。
def test_atomic_failure_keeps_old_content(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    before = store.path("testing").read_bytes()
    with patch("kama_claude.core.memory.files.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(MemoryError):
            put(store, content="changed")
    assert store.path("testing").read_bytes() == before
    assert not list(store.root.glob(".memory-*"))


# 功能：正文提交后索引失败明确返回部分提交，下次读取先修复。
# 设计：只让索引写入失败，保留真实已落盘正文以验证恢复行为。
def test_partial_commit_is_reported_and_repaired(tmp_path: Path) -> None:
    from kama_claude.core.memory.files import atomic_write

    store = memory(tmp_path)

    # 模拟索引单独写入失败，正文写入仍走真实原子提交。
    def fail_index(path: Path, text: str) -> None:
        if path.name == "MEMORY.md":
            raise MemoryError("index disk failure")
        atomic_write(path, text)

    with patch("kama_claude.core.memory.store.atomic_write", side_effect=fail_index):
        with pytest.raises(MemoryError, match="body saved, index update failed"):
            put(store)
    assert store.path("testing").exists()
    assert store.list()["total"] == 1
    assert (store.root / "MEMORY.md").exists()


# 功能：损坏正文不被静默删除或覆盖，正文分页可以读取长的手工条目。
# 设计：分别破坏固定头和扩展正文，覆盖格式与按需读取两个边界。
def test_corruption_and_body_pagination(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    path = store.path("testing")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Full details are loaded only on demand.", "x" * 5_000),
                    encoding="utf-8")
    first = store.read("testing")
    assert len(first["content"]) == 4_000 and first["next_offset"] == 4_000
    assert len(store.read("testing", offset=4_000)["content"]) == 1_000
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(MemoryError, match="header"):
        store.index()
    with pytest.raises(MemoryError):
        put(store)
    assert path.read_text(encoding="utf-8") == "broken"


# 功能：Git 子目录共享项目记忆，同名但不同路径的项目互相隔离。
# 设计：本地创建 Git 仓库，无网络、无提交，并比较后端绑定目录。
def test_project_identity(tmp_path: Path) -> None:
    root = tmp_path / "one" / "repo"
    other = tmp_path / "two" / "repo"
    root.mkdir(parents=True)
    other.mkdir(parents=True)
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    child = root / "src"
    child.mkdir()
    home = tmp_path / "home"
    a, b = MemoryStore(root, home=home), MemoryStore(child, home=home)
    assert a.project_root == root and a.root == b.root
    assert a.project_id != project_id(other)
    assert MemoryStore(other, home=home).project_root == other


# 功能：目录 junction 不能将项目记忆或全局自动入口重定向到外部。
# 设计：复用平台链接帮助函数，在无文件 symlink 权限的 Windows 仍验证目录链接。
def test_linked_memory_root_is_denied(tmp_path: Path) -> None:
    from tests.unit.test_workspace import directory_link

    store = memory(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    store.root.parent.mkdir(parents=True)
    directory_link(store.root, external)
    with pytest.raises(MemoryError, match="links"):
        put(store)
    assert not list(external.iterdir())
