from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.tools.builtin import (
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchTextTool,
    WriteFileTool,
)
from kama_claude.core.tools.files import MAX_OUTPUT_BYTES, ReadBudget
from kama_claude.core.tools.traversal import DirectoryScanner, matches_glob
from tests.unit.test_tool_contracts import execute
from tests.unit.test_workspace import directory_link, unlink_directory


# 功能：验证文件 glob 的零层 **、路径边界及普通文件名匹配
# 设计：固定输入覆盖常见源码模式，不依赖操作系统目录顺序
@pytest.mark.parametrize("path,pattern,expected", [
    ("main.py", "**/*.py", True),
    ("src/main.py", "**/*.py", True),
    ("src/main.py", "*.py", True),
    ("src/a/main.py", "src/*.py", False),
    ("src/a/main.py", "src/**/*.py", True),
    ("tests/main.py", "src/**", False),
])
def test_glob_semantics(path: str, pattern: str, expected: bool) -> None:
    assert matches_glob(path, pattern) is expected


# 功能：验证查找和目录树默认过滤生成目录与各级 ignore，且能显式查看
# 设计：根规则忽略 tmp，子规则反选 allow.tmp，使用真实嵌套目录测试规则作用域
async def test_filters_and_explicit_override(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    for name in (".git", ".venv", "node_modules", "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "code.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / ".gitignore").write_text("!allow.tmp\n", encoding="utf-8")
    (tmp_path / "src" / "allow.tmp").write_text("keep", encoding="utf-8")
    (tmp_path / "src" / "skip.tmp").write_text("skip", encoding="utf-8")
    tool = FindFilesTool(tmp_path)
    result = json.loads((await tool.invoke({"pattern": "**/*"})).content)
    assert "src/code.py" in result["files"] and "src/allow.tmp" in result["files"]
    assert "src/skip.tmp" not in result["files"]
    assert not any(path.startswith((".git/", ".venv/", "node_modules/")) for path in result["files"])
    all_files = json.loads((await tool.invoke({"pattern": "**/*", "include_ignored": True})).content)
    assert ".venv/code.py" in all_files["files"] and "src/skip.tmp" in all_files["files"]
    listing = (await ListDirTool(tmp_path).invoke({})).content
    assert ".venv" not in listing and "node_modules" not in listing
    assert "allow.tmp" in listing and "skip.tmp" not in listing


# 功能：验证 find_files 恰好达到上限不误报截断，多一条时才截断
# 设计：两个文件分别以 limit=2/1 查询，同时验证结果数量和元数据
async def test_find_result_limit(tmp_path: Path) -> None:
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    tool = FindFilesTool(tmp_path)
    complete = json.loads((await tool.invoke({"pattern": "*.py", "limit": 2})).content)
    partial = json.loads((await tool.invoke({"pattern": "*.py", "limit": 1})).content)
    assert not complete["truncated"] and len(complete["files"]) == 2
    assert partial["truncated"] and len(partial["files"]) == 1


# 功能：验证字面量、正则、大小写及前后文均返回准确行号
# 设计：同一文件包含正则元字符和中文，避免字面量被误当作正则
async def test_search_modes_and_context(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("before\nFoo.bar 中文\nafter\nfooXbar\n", encoding="utf-8")
    tool = SearchTextTool(tmp_path)
    result = json.loads((await tool.invoke({"pattern": "foo.bar", "case_sensitive": False})).content)
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["line"] == 2 and match["path"] == "sample.py"
    assert match["before"] == [{"line": 1, "text": "before"}]
    assert match["after"] == [{"line": 3, "text": "after"}]
    regular = json.loads((await tool.invoke({
        "pattern": "foo.bar", "literal": False, "case_sensitive": False, "context_lines": 0,
    })).content)
    assert [item["line"] for item in regular["matches"]] == [2, 4]
    chinese = json.loads((await tool.invoke({"pattern": "中文", "include": "*.py"})).content)
    assert chinese["matches"][0]["line"] == 2


# 功能：验证非法正则在外部路径审批之前拒绝
# 设计：同时设置外部路径与非法语法，确认没有 pending 审批或执行意图
async def test_invalid_regex_is_preflight_error(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    phases: list[str] = []
    permissions = PermissionManager(timeout_s=0.01)
    result = await execute(SearchTextTool(root), {
        "path": str(tmp_path), "pattern": "(", "literal": False,
    }, phases, permissions)
    assert result.error_type == "schema_error" and not result.outcome_unknown
    assert phases == [] and not permissions._pending


# 功能：验证病态正则有界退出，返回截断原因而不是卡住 Core
# 设计：使用典型回溯模式和足够长的失败输入，真实触发正则库超时
async def test_regex_timeout(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("a" * 50_000 + "!", encoding="utf-8")
    started = time.monotonic()
    result = json.loads((await SearchTextTool(tmp_path).invoke({
        "pattern": "(a+)+$", "literal": False,
    })).content)
    assert result["truncated"] and result["reason"] == "regex_timeout"
    assert time.monotonic() - started < 5


# 功能：验证搜索跳过二进制，命中数量及输出字节均有上限
# 设计：大量中文长行扩大输出，同时放入二进制文件检测跳过元数据
async def test_search_limits_and_binary_skip(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"\0binary")
    (tmp_path / "many.txt").write_text(("match" + "中" * 1000 + "\n") * 250, encoding="utf-8")
    output = (await SearchTextTool(tmp_path).invoke({
        "pattern": "match", "limit": 200, "context_lines": 3,
    })).content
    result = json.loads(output)
    assert result["truncated"] and len(output.encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert 0 < len(result["matches"]) < 200
    assert result["skipped_files"] == 1


# 功能：验证链接既不被搜索读取，也不递归扩大批准目录范围
# 设计：Windows 实际创建 junction，外部文件只含独特关键词，防止测试误命中
async def test_search_does_not_follow_links(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("secret-token", encoding="utf-8")
    link = root / "linked"
    directory_link(link, outside)
    try:
        found = json.loads((await FindFilesTool(root).invoke({"pattern": "*.py"})).content)
        searched = json.loads((await SearchTextTool(root).invoke({"pattern": "secret-token"})).content)
        assert found["files"] == [] and searched["matches"] == []
        assert "linked [link]" in (await ListDirTool(root).invoke({})).content
    finally:
        unlink_directory(link)


# 功能：验证分页行号、下一页、文件末尾和空范围
# 设计：含中文的三行文件逐页读取，确保没有重复或遗漏正文
async def test_read_pages_and_line_numbers(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("第一行\nsecond\nlast\n", encoding="utf-8")
    tool = ReadFileTool(tmp_path)
    first = (await tool.invoke({"path": "a.py", "max_lines": 2})).content
    second = (await tool.invoke({"path": "a.py", "start_line": 3})).content
    empty = (await tool.invoke({"path": "a.py", "start_line": 99})).content
    assert "1 | 第一行" in first and "2 | second" in first and "3 | last" not in first
    assert "next_start_line: 3" in first
    assert "3 | last" in second and "next_start_line: none" in second
    assert "(empty range)" in empty


# 功能：验证读取不调用全文件 read_bytes，CRLF 边界不会制造额外行
# 设计：单行恰好 2000 字符后接 CRLF，覆盖分块边界并阻止旧全读实现回归
async def test_read_is_bounded_and_handles_crlf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.txt").write_bytes(("x" * 2000 + "\r\nnext\r\n").encode())
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(AssertionError("full read")))
    content = (await ReadFileTool(tmp_path).invoke({"path": "a.txt"})).content
    assert "2 | next" in content and "3 |" not in content
    assert "[line truncated]" not in content


# 功能：验证读取拒绝二进制与损坏编码，并对扫描超限明确提示
# 设计：用真实文件和很小的预算分别触发三条已知失败路径
async def test_read_binary_encoding_and_scan_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, data in (("binary", b"a\0b"), ("invalid", b"\xff\xfe")):
        (tmp_path / name).write_bytes(data)
        result = await execute(ReadFileTool(tmp_path), {"path": name})
        assert result.error_type == "invalid_operation" and not result.outcome_unknown
    (tmp_path / "many.txt").write_text("line\n" * 1000, encoding="utf-8")
    monkeypatch.setattr("kama_claude.core.tools.builtin.read_file.ReadBudget", lambda: ReadBudget(max_bytes=10))
    result = await ReadFileTool(tmp_path).invoke({"path": "many.txt", "start_line": 999})
    assert "scan_bytes_limit" in result.content


# 功能：验证局部编辑保留其他内容、BOM 和 CRLF，并返回 diff
# 设计：使用带 BOM 的 Windows 文本，仅替换中间代码行，比较最终原始字节
async def test_edit_preserves_content_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_bytes(b"\xef\xbb\xbf" + b"before\r\ntimeout = 30\r\nafter\r\n")
    result = await EditFileTool(tmp_path).invoke({
        "path": "config.py", "old_text": "timeout = 30\nafter", "new_text": "timeout = 60\nafter",
    })
    assert path.read_bytes() == b"\xef\xbb\xbf" + b"before\r\ntimeout = 60\r\nafter\r\n"
    assert "-timeout = 30" in json.loads(result.content)["diff"]
    assert "+timeout = 60" in json.loads(result.content)["diff"]


# 功能：验证缺失、多重和重叠匹配均拒绝编辑，保持原文件
# 设计：aa 在 aaa 中存在两个重叠位置，不能用 str.count 的非重叠计数误放行
@pytest.mark.parametrize("content,old", [("original", "missing"), ("same same", "same"), ("aaa", "aa")])
async def test_edit_requires_unique_match(tmp_path: Path, content: str, old: str) -> None:
    target = tmp_path / "a.txt"
    target.write_text(content, encoding="utf-8")
    result = await execute(EditFileTool(tmp_path), {"path": "a.txt", "old_text": old, "new_text": "new"})
    assert result.error_type == "conflict" and not result.outcome_unknown
    assert target.read_text(encoding="utf-8") == content


# 功能：验证编辑前后原文不一致时拒绝提交，清理临时文件
# 设计：在 fsync 时模拟另一个进程修改目标，必须保留用户的新内容
async def test_edit_detects_concurrent_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "a.txt"
    target.write_text("original", encoding="utf-8")
    original_fsync = os.fsync

    # 在生成临时文件之后、替换之前制造并发修改
    def change_during_fsync(fd: int) -> None:
        original_fsync(fd)
        target.write_text("user changed", encoding="utf-8")

    monkeypatch.setattr(os, "fsync", change_during_fsync)
    result = await execute(EditFileTool(tmp_path), {"path": "a.txt", "old_text": "original", "new_text": "new"})
    assert result.error_type == "conflict" and not result.outcome_unknown
    assert target.read_text(encoding="utf-8") == "user changed"
    assert not list(tmp_path.glob(".kama-*"))


# 功能：验证原子替换失败不留下半文件，提交后结果丢失仍是未知
# 设计：分别在 replace 前后注入异常，证明不能把所有写入异常当作未执行
@pytest.mark.parametrize("after_commit", [False, True])
async def test_atomic_failure_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool) -> None:
    target = tmp_path / "a.txt"
    target.write_text("original", encoding="utf-8")
    replace = os.replace
    calls = 0

    # 精确控制最终提交点，不调用模型或外部命令
    def fail_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if after_commit:
            replace(source, destination)
        raise OSError("commit response lost")

    monkeypatch.setattr(os, "replace", fail_replace)
    result = await execute(WriteFileTool(tmp_path), {"path": "a.txt", "content": "new"})
    assert result.outcome_unknown and calls == 1
    assert target.read_text(encoding="utf-8") == ("new" if after_commit else "original")
    assert not list(tmp_path.glob(".kama-*"))


# 功能：验证新工具的工作区外访问必须逐次审批，工具级授权不能绕过
# 设计：真实目录与文件分别执行查找、搜索、编辑，两次允许都要询问，拒绝不能修改
@pytest.mark.parametrize("tool_type", [FindFilesTool, SearchTextTool, EditFileTool])
@pytest.mark.parametrize("decision", ["allow_once", "deny_once"])
async def test_external_new_tools_require_each_approval(tmp_path: Path, tool_type, decision: str) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "config.py"
    target.write_text("original", encoding="utf-8")
    manager, bus = PermissionManager(), EventBus()
    tool = tool_type(root)
    manager._persistent_always[tool.name] = "allow"
    requests: list[PermissionRequestedEvent] = []

    # 每次都显式响应单次权限，并核对绑定的实际路径
    async def respond(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            assert event.outside_workspace
            assert event.allowed_decisions == ["allow_once", "deny_once"]
            assert event.resolved_path == str(target if tool_type is EditFileTool else outside)
            manager.respond(event.tool_use_id, decision)

    bus.subscribe(respond)
    for index in range(2):
        if tool_type is FindFilesTool:
            params = {"path": str(outside), "pattern": "*.py"}
        elif tool_type is SearchTextTool:
            params = {"path": str(outside), "pattern": "original"}
        else:
            params = {"path": str(target), "old_text": "original" if index == 0 else "new",
                      "new_text": "new" if index == 0 else "again"}
        result = await execute(tool, params, manager=manager, bus=bus)
        assert result.is_error == (decision == "deny_once")
        assert not result.outcome_unknown
    assert len(requests) == 2
    assert not manager._session_always
    assert target.read_text(encoding="utf-8") == (
        "again" if tool_type is EditFileTool and decision == "allow_once" else "original"
    )


# 功能：验证扫描条目超限仍返回已经找到的路径，而不是丢弃全部部分结果
# 设计：把扫描上限缩小到三个条目，用五个真实文件精确触发边界
async def test_scan_limit_retains_partial_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(5):
        (tmp_path / f"{index}.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr("kama_claude.core.tools.traversal._MAX_SCAN_ENTRIES", 3)
    result = json.loads((await FindFilesTool(tmp_path).invoke({"pattern": "*.py"})).content)
    assert len(result["files"]) == 3
    assert result["truncated"] and result["reason"] == "scan_entries_limit"


# 功能：验证编辑缺失文件、目录、二进制和过大文件都属于明确前置错误
# 设计：修改类工具也必须在尚未写入时准确分类，不能直接当成未知副作用
@pytest.mark.parametrize("kind,expected", [
    ("missing", "not_found"), ("directory", "invalid_operation"),
    ("binary", "invalid_operation"), ("large", "resource_limit"),
])
async def test_edit_precondition_errors(tmp_path: Path, kind: str, expected: str) -> None:
    path = tmp_path / "target"
    if kind == "directory":
        path.mkdir()
    elif kind == "binary":
        path.write_bytes(b"\0binary")
    elif kind == "large":
        path.write_bytes(b"x" * (1024 * 1024 + 1))
    result = await execute(EditFileTool(tmp_path), {"path": "target", "old_text": "x", "new_text": "y"})
    assert result.error_type == expected and not result.outcome_unknown


# 功能：验证 UTF-8 字节数超限时，不创建目录或改变目标文件
# 设计：中文字符数低于 Schema 上限但字节数超过 1 MiB，必须在写入前拒绝
async def test_write_byte_limit_is_known_error(tmp_path: Path) -> None:
    result = await execute(WriteFileTool(tmp_path), {"path": "new/a.txt", "content": "中" * 400_000})
    assert result.error_type == "resource_limit" and not result.outcome_unknown
    assert not (tmp_path / "new").exists()


# 功能：验证 UTF-8 中文分页受到总输出字节数限制，并提供可继续的下一行
# 设计：用字符数不长但字节数较大的中文多行文件触发输出上限
async def test_read_output_byte_limit(tmp_path: Path) -> None:
    (tmp_path / "chinese.txt").write_text(("中" * 700 + "\n") * 200, encoding="utf-8")
    content = (await ReadFileTool(tmp_path).invoke({"path": "chinese.txt"})).content
    assert len(content.encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert "next_start_line: none" not in content and "[truncated" in content


# 功能：验证文件读取权限不足时，搜索明确报告部分结果
# 设计：在读取入口注入权限失败，不能把未读取当成完整搜索无匹配
async def test_search_unreadable_file_reports_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "hidden.txt").write_text("secret", encoding="utf-8")

    # 模拟实际打开文件时失败，不改变目录枚举
    def denied(*args, **kwargs):
        raise PermissionError("cannot read")

    monkeypatch.setattr("kama_claude.core.tools.builtin.search_text.text_lines", denied)
    result = json.loads((await SearchTextTool(tmp_path).invoke({"pattern": "secret"})).content)
    assert result["truncated"] and result["reason"] == "unreadable_entries"
    assert result["unreadable_files"] == 1


# 功能：验证无法进入的子目录不会隐藏已找到文件，也不能声称完整扫描
# 设计：仅拒绝一个真实子目录，根目录中的可见文件仍须返回
async def test_find_unreadable_directory_reports_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "denied").mkdir()
    (tmp_path / "visible.py").write_text("x", encoding="utf-8")
    original = DirectoryScanner.entries

    # 根目录正常读取，仅模拟子目录无权限
    def entries(self: DirectoryScanner, directory: Path):
        if directory.name == "denied":
            raise PermissionError("denied directory")
        return original(self, directory)

    monkeypatch.setattr(DirectoryScanner, "entries", entries)
    result = json.loads((await FindFilesTool(tmp_path).invoke({"pattern": "*.py"})).content)
    assert result["files"] == ["visible.py"] and result["truncated"]
    assert result["reason"] == "unreadable_directories"


# 功能：验证读取或覆盖目录都返回对象类型错误，不误报为权限不足或未知结果
# 设计：使用真实目录，覆盖 Windows 打开目录时可能抛 PermissionError 的平台差异
@pytest.mark.parametrize("tool_type,params", [
    (ReadFileTool, {"path": "."}),
    (WriteFileTool, {"path": ".", "content": "x"}),
])
async def test_file_tools_reject_directories(tmp_path: Path, tool_type, params) -> None:
    result = await execute(tool_type(tmp_path), params)
    assert result.error_type == "invalid_operation" and not result.outcome_unknown
