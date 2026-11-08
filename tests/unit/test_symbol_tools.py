from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig, ExecutionConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.subagent.roles import BACKGROUND_TOOLS
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.base import BaseTool
from kama_claude.core.tools.builtin import (
    FindFilesTool,
    GetFileOutlineTool,
    ReadSymbolTool,
    SearchSymbolTool,
    SearchTextTool,
)
from kama_claude.core.tools.factory import builtin_tools
from kama_claude.core.tools.files import ReadBudget
from kama_claude.core.tools.output import MAX_RESULT_CHARS
from kama_claude.core.tools.symbols import parse_source
from tests.unit.test_tool_contracts import execute
from tests.unit.test_workspace import directory_link, unlink_directory


# 真实语法包提取定义，检查限定名、类型和源码范围，而不是只 mock 解析结果。
@pytest.mark.parametrize("suffix,source,expected", [
    (".py", "@decorate\nclass Service:\n    @memo\n    async def handle(self, x):\n"
     "        def inner():\n            return '中文'\n        return x\n",
     [("Service", "class", 1, 7), ("Service.handle", "method", 3, 7),
      ("Service.handle.inner", "function", 5, 6)]),
    (".js", "export function find(x) { return x; }\n"
     "export const load = (x) => x + 1;\nclass Store { get(x) { return x; } }\n",
     [("find", "function", 1, 1), ("load", "function", 2, 2),
      ("Store", "class", 3, 3), ("Store.get", "method", 3, 3)]),
    (".jsx", "export const View = () => <div>中文</div>;\n",
     [("View", "function", 1, 1)]),
    (".ts", "export interface API { fetch(x: number): string; }\n"
     "type Name = string;\nenum State { Ready }\nconst next = (x: number): number => x + 1;\n",
     [("API", "interface", 1, 1), ("API.fetch", "method", 1, 1),
      ("Name", "type", 2, 2), ("State", "enum", 3, 3), ("next", "function", 4, 4)]),
    (".tsx", "export const Page = ({name}: {name: string}) => <p>{name}</p>;\n",
     [("Page", "function", 1, 1)]),
])
def test_real_language_definitions(tmp_path: Path, suffix, source, expected) -> None:
    path = tmp_path / ("sample" + suffix)
    path.write_text(source, encoding="utf-8")
    parsed = parse_source(path, ReadBudget())
    assert parsed.reason is None
    assert [(s.qualified_name, s.kind, s.start_line, s.end_line) for s in parsed.symbols] == expected
    assert all("return" not in s.signature for s in parsed.symbols)


async def test_outline_reads_only_requested_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "one.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr("kama_claude.core.tools.traversal.DirectoryScanner.files",
                        lambda _: pytest.fail("single-file tools must not scan the repository"))
    result = json.loads((await GetFileOutlineTool(tmp_path).invoke({"path": "one.py"})).content)
    assert result["root"] == str(tmp_path)
    assert result["symbols"][0]["signature"] == "def one():"
    assert not result["truncated"]
    read = json.loads((await ReadSymbolTool(tmp_path).invoke({
        "path": "one.py", "name": "one", "expected_hash": result["file_hash"],
    })).content)
    assert read["source"] == (tmp_path / "one.py").read_bytes().decode()


async def test_search_exact_partial_kinds_and_ignore(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("class A:\n    def handle(self): pass\n", encoding="utf-8")
    (tmp_path / "other.ts").write_text("function handleMore() {}", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("def handle(): pass", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "text.txt").write_text("handle", encoding="utf-8")
    tool = SearchSymbolTool(tmp_path)
    exact = json.loads((await tool.invoke({"name": "handle"})).content)
    assert [(s["path"], s["qualified_name"]) for s in exact["symbols"]] == [("src/a.py", "A.handle")]
    assert exact["unsupported_files"] >= 1 and not exact["truncated"]
    partial = json.loads((await tool.invoke({"name": "handle", "match": "partial"})).content)
    assert len(partial["symbols"]) == 2
    methods = json.loads((await tool.invoke({"name": "A.handle", "kind": "method"})).content)
    assert len(methods["symbols"]) == 1
    all_files = json.loads((await tool.invoke({"name": "handle", "include_ignored": True})).content)
    assert len(all_files["symbols"]) == 2
    empty = json.loads((await tool.invoke({"name": "HANDLE"})).content)
    assert empty["symbols"] == [] and not empty["truncated"]


async def test_symbol_pagination_and_changed_file(tmp_path: Path) -> None:
    path = tmp_path / "one.py"
    original = "def one():\n    x = 1\n    return x\n"
    path.write_bytes(b"\xef\xbb\xbf" + original.replace("\n", "\r\n").encode())
    tool = ReadSymbolTool(tmp_path)
    first = json.loads((await tool.invoke({"path": "one.py", "name": "one", "max_lines": 2})).content)
    assert first["next_start_line"] == 3 and first["end_line"] == 2
    params = {"path": "one.py", "name": "one", "start_line": 3, "expected_hash": first["file_hash"]}
    second = json.loads((await tool.invoke(params)).content)
    assert second["source"] == "    return x\r\n" and second["next_start_line"] is None
    assert first["source"] + second["source"] == original.replace("\n", "\r\n")
    path.write_text(original + "# changed\n", encoding="utf-8")
    changed = await execute(tool, params)
    assert changed.error_type == "conflict" and not changed.outcome_unknown


async def test_ambiguous_symbol_requires_selection(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\ndef f():\n    return 2\n", encoding="utf-8")
    tool = ReadSymbolTool(tmp_path)
    ambiguous = json.loads((await tool.invoke({"path": "a.py", "name": "f"})).content)
    assert ambiguous["status"] == "ambiguous" and len(ambiguous["symbols"]) == 2
    selected = json.loads((await tool.invoke({"path": "a.py", "name": "f", "start_line": 3})).content)
    assert "return 2" in selected["source"] and "return 1" not in selected["source"]
    missing = await execute(tool, {"path": "a.py", "name": "absent"})
    assert missing.error_type == "not_found" and not missing.outcome_unknown


async def test_syntax_error_is_partial_and_unsupported_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def good(): pass\ndef broken(\n", encoding="utf-8")
    parsed = json.loads((await GetFileOutlineTool(tmp_path).invoke({"path": "bad.py"})).content)
    assert parsed["truncated"] and parsed["reason"] == "parse_error"
    assert any(s["name"] == "good" for s in parsed["symbols"])
    incomplete = await execute(ReadSymbolTool(tmp_path), {"path": "bad.py", "name": "missing"})
    assert incomplete.error_type == "invalid_operation" and "partial parse" in incomplete.content
    (tmp_path / "one.rb").write_text("class Service; end", encoding="utf-8")
    result = await execute(GetFileOutlineTool(tmp_path), {"path": "one.rb"})
    assert result.error_type == "invalid_operation" and "search_text" in result.content
    result = await execute(GetFileOutlineTool(tmp_path), {"path": "missing.py"})
    assert result.error_type == "not_found"
    result = await execute(GetFileOutlineTool(tmp_path), {"path": "missing.go"})
    assert result.error_type == "not_found"


async def test_bounded_json_survives_output_preparer(tmp_path: Path) -> None:
    (tmp_path / "many.py").write_text("\n".join(f"def f_{i}(): pass" for i in range(200)), encoding="utf-8")
    preparer = ToolOutputPreparer(CompactionConfig(tool_result_limit=100, tool_result_keep=50), None)
    tools_and_params = [
        (GetFileOutlineTool(tmp_path), {"path": "many.py", "limit": 200}),
        (SearchSymbolTool(tmp_path), {"name": "f_", "match": "partial", "limit": 200}),
        (SearchTextTool(tmp_path), {"pattern": "def", "limit": 200}),
        (FindFilesTool(tmp_path), {"pattern": "*.py"}),
    ]
    for tool, params in tools_and_params:
        result = await tool.invoke(params)
        assert len(result.content) <= MAX_RESULT_CHARS and result.preserve_in_context
        projected = preparer.prepare("call", result)
        assert json.loads(projected) == json.loads(result.content)
    partial = json.loads((await GetFileOutlineTool(tmp_path).invoke({"path": "many.py", "limit": 200})).content)
    assert partial["reason"] == "output_limit" and 0 < len(partial["symbols"]) < 200


async def test_large_symbol_output_pages_without_cutting_source(tmp_path: Path) -> None:
    text = "def f():\n" + "".join(f"    x = '{i} 中文'\n" for i in range(600))
    (tmp_path / "big.py").write_bytes(text.encode("utf-8"))
    tool = ReadSymbolTool(tmp_path)
    pages: list[str] = []
    params = {"path": "big.py", "name": "f", "max_lines": 2000}
    for _ in range(10):
        result = json.loads((await tool.invoke(params)).content)
        pages.append(result["source"])
        if result["next_start_line"] is None:
            break
        assert result["reason"] == "output_limit"
        params.update(start_line=result["next_start_line"], expected_hash=result["file_hash"])
    assert "".join(pages) == text


async def test_unicode_separator_is_not_a_source_newline(tmp_path: Path) -> None:
    text = "const f = () => 'a\u2028b';\nfunction next() {}\n"
    (tmp_path / "one.js").write_bytes(text.encode("utf-8"))
    result = json.loads((await ReadSymbolTool(tmp_path).invoke({"path": "one.js", "name": "f"})).content)
    assert result["source"] == "const f = () => 'a\u2028b';\n"


async def test_long_line_is_explicit_error(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("def f(): return '" + "x" * 9000 + "'\n", encoding="utf-8")
    result = await execute(ReadSymbolTool(tmp_path), {"path": "one.py", "name": "f"})
    assert result.error_type == "resource_limit" and "read_file" in result.content


async def test_partial_scan_and_result_limit(tmp_path: Path, monkeypatch) -> None:
    for i in range(4):
        (tmp_path / f"{i}.py").write_text("def f(): pass\n", encoding="utf-8")
    tool = SearchSymbolTool(tmp_path)
    full = json.loads((await tool.invoke({"name": "f", "limit": 4})).content)
    limited = json.loads((await tool.invoke({"name": "f", "limit": 3})).content)
    assert not full["truncated"] and limited["reason"] == "result_limit"
    monkeypatch.setattr("kama_claude.core.tools.builtin.search_symbol.ReadBudget",
                        lambda: ReadBudget(max_bytes=10))
    partial = json.loads((await tool.invoke({"name": "absent"})).content)
    assert partial["truncated"] and "scan_bytes_limit" in partial["reason"]


@pytest.mark.parametrize("tool_type,params", [
    (GetFileOutlineTool, {"path": "a.py", "limit": True}),
    (SearchSymbolTool, {"name": "f", "match": "regex"}),
    (SearchSymbolTool, {"name": "f", "kind": "variable"}),
    (ReadSymbolTool, {"path": "a.py", "name": "f", "expected_hash": "fake"}),
    (ReadSymbolTool, {"path": "a.py", "name": "f", "approved_path": "a.py"}),
    (SearchTextTool, {"pattern": "f", "output_mode": "bad"}),
])
async def test_strict_parameters_before_approval(tmp_path: Path, tool_type, params) -> None:
    phases: list[str] = []
    result = await execute(tool_type(tmp_path), params, phases, PermissionManager(timeout_s=0.01))
    assert result.error_type == "schema_error" and phases == []


@pytest.mark.parametrize("tool_type,params", [
    (GetFileOutlineTool, {}), (SearchSymbolTool, {"name": "f"}),
    (ReadSymbolTool, {"name": "f"}),
])
async def test_external_approval_and_deny(tmp_path: Path, tool_type, params) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "a.py"
    target.write_text("def f(): pass\n", encoding="utf-8")
    manager = PermissionManager()
    bus = EventBus()
    requests = []

    async def respond(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            assert event.resolved_path == str(target)
            assert event.allowed_decisions == ["allow_once", "deny_once"]
            manager.respond(event.request_id, "allow_once", session_id=event.session_id,
                            run_id=event.run_id)

    bus.subscribe(respond)
    for _ in range(2):
        result = await execute(tool_type(root), {**params, "path": str(target)}, manager=manager, bus=bus)
        assert not result.is_error
    assert len(requests) == 2
    denied = PermissionManager(policies={tool_type.name: ToolPolicy(PermissionDecision.DENY)})
    phases: list[str] = []
    result = await execute(tool_type(root), {**params, "path": str(target)}, phases, denied)
    assert result.error_type == "permission_denied" and "executing" not in phases


async def test_search_skips_directory_links(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("def secret(): pass", encoding="utf-8")
    link = root / "link"
    directory_link(link, outside)
    try:
        result = json.loads((await SearchSymbolTool(root).invoke({"name": "secret"})).content)
        assert result["symbols"] == []
    finally:
        unlink_directory(link)


# 功能：验证默认注册保留大纲和符号阅读，不再暴露符号搜索。
# 设计：检查主子 Agent 共用工厂及后台清单，避免仅修改单个调用入口。
def test_shared_registration_and_background_capabilities(tmp_path: Path) -> None:
    tools = {tool.name: tool for tool in builtin_tools(tmp_path, ExecutionConfig(), TaskManager(tmp_path))}
    assert "search_symbol" not in tools and "search_symbol" not in BACKGROUND_TOOLS
    for name in ("get_file_outline", "read_symbol"):
        assert name in tools and name in BACKGROUND_TOOLS
        assert isinstance(tools[name], BaseTool)


async def test_search_files_counts_lines_not_occurrences(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit hit\nnone\nhit\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hit\n", encoding="utf-8")
    tool = SearchTextTool(tmp_path)
    result = json.loads((await tool.invoke({"pattern": "hit", "output_mode": "files"})).content)
    assert result["files"] == [
        {"path": "a.txt", "matching_lines": 2, "count_complete": True},
        {"path": "b.txt", "matching_lines": 1, "count_complete": True},
    ]
    limited = json.loads((await tool.invoke({"pattern": "hit", "output_mode": "files", "limit": 1})).content)
    assert limited["reason"] == "result_limit" and len(limited["files"]) == 1
    assert limited["files"][0]["count_complete"] is True


async def test_search_files_partial_counts_are_lower_bounds(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit\n" + "hit" * 30_000 + "\nhit\n", encoding="utf-8")
    result = json.loads((await SearchTextTool(tmp_path).invoke({"pattern": "hit", "output_mode": "files"})).content)
    assert result["truncated"] and result["reason"] == "line_limit"
    assert result["files"] == [{"path": "a.txt", "matching_lines": 2, "count_complete": False}]


@pytest.mark.parametrize("tool_type,params", [
    (GetFileOutlineTool, {}), (SearchSymbolTool, {"name": "f"}),
    (ReadSymbolTool, {"name": "f"}),
])
async def test_link_target_change_during_approval(tmp_path: Path, tool_type, params) -> None:
    root, first, second = (tmp_path / name for name in ("root", "first", "second"))
    for folder in (root, first, second):
        folder.mkdir()
        (folder / "a.py").write_text("def f(): pass", encoding="utf-8")
    link = root / "alias"
    directory_link(link, first)
    manager, bus = PermissionManager(), EventBus()

    async def swap(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            unlink_directory(link)
            directory_link(link, second)
            manager.respond(event.request_id, "allow_once", session_id=event.session_id,
                            run_id=event.run_id)

    bus.subscribe(swap)
    phases: list[str] = []
    try:
        result = await execute(tool_type(root), {**params, "path": "alias/a.py"}, phases, manager, bus)
        assert result.error_type == "permission_denied" and not result.outcome_unknown
        assert phases == ["awaiting_approval"]
    finally:
        unlink_directory(link)


@pytest.mark.parametrize("data,error", [
    (b"\xff", "invalid_operation"), (b"\0", "invalid_operation"),
    (b"#" * (1024 * 1024 + 1), "resource_limit"),
], ids=["invalid_utf8", "binary", "oversized"])
async def test_source_precondition_errors(tmp_path: Path, data, error) -> None:
    (tmp_path / "bad.py").write_bytes(data)
    result = await execute(GetFileOutlineTool(tmp_path), {"path": "bad.py"})
    assert result.error_type == error and not result.outcome_unknown


async def test_files_count_is_partial_when_io_budget_expires(tmp_path: Path, monkeypatch) -> None:
    from kama_claude.core.tools.files import TextLine

    (tmp_path / "a.txt").write_text("hit\n" * 10, encoding="utf-8")

    def partial(path, budget):
        yield TextLine(1, "hit", False)
        budget.reason = "scan_bytes_limit"

    monkeypatch.setattr("kama_claude.core.tools.builtin.search_text.text_lines", partial)
    result = json.loads((await SearchTextTool(tmp_path).invoke({"pattern": "hit", "output_mode": "files"})).content)
    assert result["files"][0] == {"path": "a.txt", "matching_lines": 1, "count_complete": False}
    assert result["truncated"] and result["reason"] == "scan_bytes_limit"


async def test_partial_decode_failure_marks_file_count_incomplete(tmp_path: Path, monkeypatch) -> None:
    from kama_claude.core.tools.files import TextLine

    (tmp_path / "a.txt").write_text("hit", encoding="utf-8")

    def broken(path, budget):
        yield TextLine(1, "hit", False)
        raise UnicodeError("bad tail")

    monkeypatch.setattr("kama_claude.core.tools.builtin.search_text.text_lines", broken)
    result = json.loads((await SearchTextTool(tmp_path).invoke({"pattern": "hit", "output_mode": "files"})).content)
    assert result["truncated"] and result["reason"] == "unreadable_entries"
    assert result["files"][0]["count_complete"] is False
