from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kama_claude.core.memory.model import MemoryError
from kama_claude.core.memory.store import MemoryStore


# 绑定全部三层到临时目录，不接触真实用户记忆
def memory(tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        tmp_path / "project", tmp_path / "session", global_dir=tmp_path / "global",
    )


# 写入一条内容明确的记忆，测试只覆盖行为而不重复模板拼接实现
def put(store: MemoryStore, scope="project", key="python", content="Python 3.12", **kwargs):
    return store.upsert(scope, key, summary="Python 版本", content=content, source="s / r", **kwargs)


# 功能：验证只读操作不创建目录，保存后只有人类可读 Markdown
# 设计：真实磁盘往返并加入正文标题、分隔线和代码块，排除解析正文为元数据的问题
def test_markdown_roundtrip_and_read_only_initialization(tmp_path: Path) -> None:
    store = memory(tmp_path)
    assert store.list("project")["entries"] == []
    assert not (tmp_path / "project").exists()
    body = "Python 3.12\n\n## 正文标题\n\n---\n\n```python\nprint('hi')\n```"
    assert put(store, content=body)["status"] == "created"
    entry = store.get("project", "python")
    assert entry.content == body and entry.summary == "Python 版本"
    path = store.directory("project") / "python.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# python\n\n- 摘要：") and "- 来源：s / r" in text
    assert not list(tmp_path.rglob("*.json"))
    assert [p.name for p in path.parent.iterdir()] == ["python.md"]


# 功能：验证同 key 更新、固定状态保留以及重复内容不改文件
# 设计：第二次写入时令原子替换必定失败，确认幂等路径根本没有写盘
def test_update_and_duplicate_are_idempotent(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store, pinned=True)
    path = store.directory("project") / "python.md"
    original = path.read_bytes()
    with patch("kama_claude.core.memory.store.os.replace", side_effect=OSError("must not write")):
        result = put(store)
    assert result["status"] == "unchanged" and path.read_bytes() == original
    assert put(store, content="Python 3.13")["status"] == "updated"
    assert store.get("project", "python").pinned is True
    assert [p.name for p in path.parent.iterdir()] == ["python.md"]


# 功能：验证三层互相隔离，其他项目和会话只能共享全局条目
# 设计：相同 key 分别写入三个作用域，再创建另一套 workspace/session 读取
def test_scope_project_and_session_isolation(tmp_path: Path) -> None:
    store = memory(tmp_path)
    for scope in ("global", "project", "session"):
        put(store, scope, content=scope)
    other = MemoryStore(tmp_path / "other", tmp_path / "other-session", global_dir=tmp_path / "global")
    assert other.get("global", "python").content == "global"
    assert other.list("project")["entries"] == [] and other.list("session")["entries"] == []
    assert store.get("project", "python").content == "project"


@pytest.mark.parametrize("key", ["..", "../x", "a/b", "a\\b", "CON", "COM1", ".hidden", "", "a" * 65])
# 功能：验证 key 无法遍历目录，也不会产生 Windows 保留文件名
# 设计：覆盖 Windows 与 POSIX 共同的危险名称，并确认失败前没有创建目录
def test_invalid_keys_do_not_write(tmp_path: Path, key: str) -> None:
    with pytest.raises(MemoryError, match="invalid memory key"):
        put(memory(tmp_path), key=key)
    assert not (tmp_path / "project").exists()


# 功能：验证原子替换失败保留旧文档，临时文件被清理
# 设计：先保存有效记忆，再精确阻断替换，防止半份 Markdown 覆盖原文
def test_failed_replace_preserves_previous_file(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    path = store.directory("project") / "python.md"
    original = path.read_bytes()
    with patch("kama_claude.core.memory.store.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(MemoryError, match="disk full"):
            put(store, content="new")
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".memory-*"))


# 功能：验证坏文件被列为错误，指定读取、更新和删除均不覆盖它
# 设计：同时保留一个有效条目，确保单份损坏不会隐藏整个作用域
def test_corrupt_document_is_reported_and_preserved(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store)
    path = store.directory("project") / "broken.md"
    path.write_text("unstructured old text", encoding="utf-8")
    result = store.list("project")
    assert result["total"] == 1 and result["warnings"]
    for action in (
        lambda: store.get("project", "broken"),
        lambda: put(store, key="broken"),
        lambda: store.delete("project", "broken"),
    ):
        with pytest.raises(MemoryError):
            action()
    assert path.read_text(encoding="utf-8") == "unstructured old text"


# 功能：验证列表只带摘要，关键词可匹配正文，手工长正文可以按段读完
# 设计：手工扩展合法模板正文超过工具写入上限，覆盖按需读取的续读位置
def test_search_index_and_long_body_pagination(tmp_path: Path) -> None:
    store = memory(tmp_path)
    put(store, content="KEYWORD")
    path = store.directory("project") / "python.md"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("中" * 8_100)
    result = store.list("project", query="keyword")
    assert result["total"] == 1 and "content" not in result["entries"][0]
    chunks = []
    offset = 0
    while True:
        result = store.read("project", "python", offset=offset)
        assert len(result["content"]) <= 4_000
        chunks.append(result["content"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert "".join(chunks) == store.get("project", "python").content


# 功能：验证重复删除安全，且已删除条目不会从旧 notes 文件重新导入
# 设计：在旁边放旧笔记，连续删除后再次扫描，要求只认新目录里的 Markdown
def test_delete_does_not_import_old_notes(tmp_path: Path) -> None:
    store = memory(tmp_path)
    (tmp_path / "session").mkdir()
    (tmp_path / "session" / "notes.md").write_text("old remembered fact", encoding="utf-8")
    put(store, "session")
    assert store.delete("session", "python")["status"] == "deleted"
    assert store.delete("session", "python")["status"] == "already_absent"
    assert store.list("session")["total"] == 0
    assert (tmp_path / "session" / "notes.md").read_text() == "old remembered fact"


@pytest.mark.parametrize("summary,content", [("", "x"), ("a\nb", "x"), ("x" * 161, "x"), ("x", "中" * 4_001)])
# 功能：验证摘要与正文限制在写盘前生效，避免把无效条目留在文件系统
# 设计：包含空摘要、换行、超长中英文，检查统一的业务校验边界
def test_write_limits(tmp_path: Path, summary: str, content: str) -> None:
    with pytest.raises(MemoryError):
        memory(tmp_path).upsert("project", "x", summary=summary, content=content, source="s / r")
