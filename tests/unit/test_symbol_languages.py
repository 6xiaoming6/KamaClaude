from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.tools.builtin import GetFileOutlineTool, ReadSymbolTool, SearchSymbolTool
from kama_claude.core.tools.files import ReadBudget
from kama_claude.core.tools.symbols import parse_source
from tests.unit.test_tool_contracts import execute

CPP = (
    "namespace demo {\n"
    "template<typename T> class Store {\n"
    "public:\n"
    " Store() {} ~Store() {}\n"
    " virtual T get(int x) const { return {}; }\n"
    " T other(int x) const;\n"
    "};\n"
    "int Store<int>::other(int x) const { return x; }\n"
    "using Name = int;\n"
    "typedef int Number;\n"
    "int free_function(int x);\n"
    "int (*callback)(int);\n"
    "int a, other_free(int x);\n"
    "}\n"
)
JAVA = (
    "package demo;\n"
    "public class Service<T> {\n"
    " public Service() {}\n"
    ' @Override public String get(int n) { return "中文"; }\n'
    " interface API { void go(); }\n"
    "}\n"
    "record Pair(int a, int b) { Pair { } }\n"
    "@interface Label { String value(); }\n"
    "enum State { READY; public int code() { return 1; } }\n"
)
RUST = (
    "mod demo {\n"
    "#[derive(Debug)]\n"
    "pub struct Store<T> { value: T }\n"
    "trait Api { fn get(&self) -> i32; fn fallback(&self) -> i32 { 0 } }\n"
    "impl<T> Store<T> { pub fn new(value: T) -> Self { Self { value } } }\n"
    "impl Api for Store<i32> { fn get(&self) -> i32 { self.value } }\n"
    "pub enum State { Ready }\n"
    "type Name = String;\n"
    "pub fn free<T>(x: T) -> T { x }\n"
    "}\n"
)
GO = (
    "package demo\n"
    "type Store[T any] struct { value T }\n"
    "type Api interface { Get(x int) string; Close() }\n"
    "type Alias = string\n"
    "type Number int\n"
    "func New[T any](value T) *Store[T] { return &Store[T]{value} }\n"
    'func (s *Store[T]) Get(x int) string { return "中文" }\n'
)


@pytest.mark.parametrize("filename,source,expected", [
    ("sample.cpp", CPP, [
        ("demo::Store", "class", 2, 7), ("demo::Store::Store", "method", 4, 4),
        ("demo::Store::~Store", "method", 4, 4), ("demo::Store::get", "method", 5, 5),
        ("demo::Store::other", "method", 6, 6), ("demo::Store::other", "method", 8, 8),
        ("demo::Name", "type", 9, 9), ("demo::Number", "type", 10, 10),
        ("demo::free_function", "function", 11, 11), ("demo::other_free", "function", 13, 13),
    ]),
    ("Sample.java", JAVA, [
        ("Service", "class", 2, 6), ("Service.Service", "method", 3, 3),
        ("Service.get", "method", 4, 4), ("Service.API", "interface", 5, 5),
        ("Service.API.go", "method", 5, 5), ("Pair", "class", 7, 7),
        ("Pair.Pair", "method", 7, 7), ("Label", "interface", 8, 8),
        ("Label.value", "method", 8, 8), ("State", "enum", 9, 9),
        ("State.code", "method", 9, 9),
    ]),
    ("sample.rs", RUST, [
        ("demo::Store", "class", 2, 3), ("demo::Api", "interface", 4, 4),
        ("demo::Api::get", "method", 4, 4), ("demo::Api::fallback", "method", 4, 4),
        ("demo::Store::new", "method", 5, 5), ("demo::Store::get", "method", 6, 6),
        ("demo::State", "enum", 7, 7), ("demo::Name", "type", 8, 8),
        ("demo::free", "function", 9, 9),
    ]),
    ("sample.go", GO, [
        ("Store", "class", 2, 2), ("Api", "interface", 3, 3),
        ("Api.Get", "method", 3, 3), ("Api.Close", "method", 3, 3),
        ("Alias", "type", 4, 4), ("Number", "type", 5, 5),
        ("New", "function", 6, 6), ("Store.Get", "method", 7, 7),
    ]),
])
# 功能：验证四种真实语法包提取常见声明、限定名和包含首尾的源码范围。
# 设计：每种语言使用包含多个声明形式的源码，严格核对完整列表，排除重复和误识别。
def test_new_language_definitions(tmp_path, filename, source, expected):
    path = tmp_path / filename
    path.write_bytes(source.encode("utf-8"))
    result = parse_source(path, ReadBudget())
    assert result.reason is None
    assert [(s.qualified_name, s.kind, s.start_line, s.end_line) for s in result.symbols] == expected
    assert all("return " not in s.signature for s in result.symbols)


@pytest.mark.parametrize("filename,source,name,kind", [
    ("answer.cpp", "namespace app {\nint answer() {\n return 42;\n}\n}\n", "app::answer", "function"),
    ("Answer.java", "class Answer {\n int answer() {\n  return 42;\n }\n}\n", "Answer.answer", "method"),
    ("answer.rs", "mod app {\npub fn answer() -> i32 {\n 42\n}\n}\n", "app::answer", "function"),
    ("answer.go", "package app\nfunc answer() int {\n return 42\n}\n", "answer", "function"),
])
# 功能：验证四种语言均可从大纲定位、精确搜索到按哈希分页读取源码。
# 设计：经真实 FileTool 入口读取 CRLF 和 BOM 文件，检查两页拼接与原始源码一致。
async def test_new_language_tools_and_pages(tmp_path, filename, source, name, kind):
    path = tmp_path / filename
    original = source.replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + original.encode())
    outline = json.loads((await GetFileOutlineTool(tmp_path).invoke({"path": filename})).content)
    symbol = next(s for s in outline["symbols"] if s["qualified_name"] == name)
    search = json.loads((await SearchSymbolTool(tmp_path).invoke({
        "name": name, "kind": kind,
    })).content)
    assert search["symbols"] == [symbol]
    reader = ReadSymbolTool(tmp_path)
    first = json.loads((await reader.invoke({
        "path": filename, "name": name, "max_lines": 1,
        "expected_hash": outline["file_hash"],
    })).content)
    assert first["next_start_line"] == symbol["start_line"] + 1
    second = json.loads((await reader.invoke({
        "path": filename, "name": name, "start_line": first["next_start_line"],
        "expected_hash": first["file_hash"],
    })).content)
    assert second["next_start_line"] is None
    expected = "".join(original.splitlines(keepends=True)[symbol["start_line"] - 1:symbol["end_line"]])
    assert first["source"] + second["source"] == expected


@pytest.mark.parametrize("suffix,source,name", [
    (".cpp", "int work(int x) { return x; }\nint work(double x) { return 2; }\n", "work"),
    (".java", "class A {\n int work(int x) { return x; }\n int work(String x) { return 2; }\n}\n", "A.work"),
    (".rs", "struct A;\ntrait One { fn work(); }\ntrait Two { fn work(); }\n"
     "impl One for A { fn work() {} }\nimpl Two for A { fn work() {} }\n", "A::work"),
])
# 功能：验证 C++/Java 重载和 Rust 不同 trait 实现不会被静默合并。
# 设计：相同限定名返回歧义候选，再用第二个候选的真实起始行选择源码。
async def test_overloads_require_explicit_selection(tmp_path, suffix, source, name):
    path = tmp_path / ("sample" + suffix)
    path.write_text(source, encoding="utf-8")
    reader = ReadSymbolTool(tmp_path)
    ambiguous = json.loads((await reader.invoke({"path": path.name, "name": name})).content)
    assert ambiguous["status"] == "ambiguous" and len(ambiguous["symbols"]) == 2
    selected = ambiguous["symbols"][1]
    result = json.loads((await reader.invoke({
        "path": path.name, "name": name, "start_line": selected["start_line"],
    })).content)
    assert result["status"] == "ok" and result["symbols"] == [selected]


@pytest.mark.parametrize("suffix", [".cpp", ".cc", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".tpp", ".C"])
# 功能：验证 C++ 常见源文件和头文件扩展名在目录搜索和单文件解析中一致。
# 设计：同一个普通函数覆盖扩展名分派，特别检查大写 .C 不被转成不支持的 .c。
async def test_cpp_extensions(tmp_path, suffix):
    path = tmp_path / ("sample" + suffix)
    path.write_text("inline int answer() { return 42; }\n", encoding="utf-8")
    result = json.loads((await SearchSymbolTool(tmp_path).invoke({"name": "answer"})).content)
    assert result["symbols"][0]["path"] == path.name
    assert result["unsupported_files"] == 0 and not result["truncated"]


@pytest.mark.parametrize("suffix,source", [
    (".cpp", "int good() { return 1; }\nclass Broken {\n"),
    (".java", "class Good { void good() {} }\nclass Broken {\n"),
    (".rs", "fn good() {}\nfn broken(\n"),
    (".go", "package main\nfunc good() {}\nfunc broken(\n"),
])
# 功能：验证新增语言的损坏源码明确报告部分解析，且仍保留已识别定义。
# 设计：完整声明之后添加未结束语法，避免把解析错误误报为空且完整的大纲。
async def test_new_language_parse_errors_are_partial(tmp_path, suffix, source):
    path = tmp_path / ("sample" + suffix)
    path.write_text(source, encoding="utf-8")
    result = json.loads((await GetFileOutlineTool(tmp_path).invoke({"path": path.name})).content)
    assert result["truncated"] and result["reason"] == "parse_error"
    assert any(s["name"] == "good" for s in result["symbols"])


# 功能：验证 Rust 属性属于类型的读取范围，内部字段不混入签名。
# 设计：分别核对起止行、签名和读取原文，防止把前置属性误用作定义结束位置。
async def test_rust_attributes_are_read_with_definition(tmp_path: Path) -> None:
    source = "#[derive(Debug)]\n#[repr(C)]\npub struct Item {\n value: i32,\n}\n"
    (tmp_path / "item.rs").write_bytes(source.encode("utf-8"))
    result = json.loads((await ReadSymbolTool(tmp_path).invoke({
        "path": "item.rs", "name": "Item",
    })).content)
    assert result["start_line"] == 1 and result["end_line"] == 5
    assert result["source"] == source
    assert result["symbols"][0]["signature"] == "pub struct Item"


# 功能：验证 Go 分组类型、指针接收者和多层泛型接收者保留正确归属。
# 设计：将命名接口成员与结构体方法混放，检查同名方法不会被归入错误类型。
def test_go_grouped_types_and_receivers(tmp_path: Path) -> None:
    source = (
        "package main\ntype (\n Store[T any] struct { value T }\n"
        " API interface { Get() int }\n Number int\n Alias = string\n)\n"
        "func (s *Store[T]) Get() int { return 1 }\n"
    )
    path = tmp_path / "sample.go"
    path.write_text(source, encoding="utf-8")
    parsed = parse_source(path, ReadBudget())
    assert parsed.reason is None
    assert [s.qualified_name for s in parsed.symbols] == [
        "Store", "API", "API.Get", "Number", "Alias", "Store.Get",
    ]


# 功能：验证 C++ 常见运算符、默认析构、模板和并列声明均可定位，函数指针不误报。
# 设计：在嵌套命名空间中混合相似声明，严格检查方法及函数名，覆盖声明器分支。
def test_cpp_declarators_and_templates(tmp_path: Path) -> None:
    source = (
        "namespace a::b {\nstruct Box {\n explicit Box(int n) : value(n) {}\n"
        " ~Box() = default;\n int operator()(int n) const { return n; }\n"
        " bool operator==(const Box&) const = default;\n"
        " operator bool() const { return true; }\n"
        " int* pointer() { return nullptr; }\nprivate: int value;\n};\n"
        "template<class T> T helper(T x);\n"
        "template<class T> T identity(T x) { return x; }\n"
        "int a(int), b(double);\nint (*callback)(int);\n"
        "void f(void (*callback)(int));\n}\n"
    )
    path = tmp_path / "sample.cpp"
    path.write_text(source, encoding="utf-8")
    parsed = parse_source(path, ReadBudget())
    assert parsed.reason is None
    assert [s.qualified_name for s in parsed.symbols] == [
        "a::b::Box", "a::b::Box::Box", "a::b::Box::~Box", "a::b::Box::operator()",
        "a::b::Box::operator==", "a::b::Box::operator bool", "a::b::Box::pointer",
        "a::b::helper", "a::b::identity", "a::b::a", "a::b::b", "a::b::f",
    ]
    assert all(s.kind == "method" for s in parsed.symbols[1:7])
    assert all("return " not in s.signature for s in parsed.symbols)


# 功能：验证 Rust 限定类型 impl 归属和方法内嵌套函数不会混为同级方法。
# 设计：混合 self/crate 路径与两个 trait 实现，保留真实重名并检查最内层词法作用域。
def test_rust_qualified_impl_and_nested_functions(tmp_path: Path) -> None:
    source = (
        "mod nested {\nstruct Item;\ntrait A { fn f(); }\ntrait B { fn f(); }\n"
        "impl A for self::Item { fn f() {} }\n"
        "impl B for crate::nested::Item { fn f() {} }\n"
        "impl Item { fn work() { fn inner() {} } }\n}\n"
    )
    path = tmp_path / "sample.rs"
    path.write_text(source, encoding="utf-8")
    parsed = parse_source(path, ReadBudget())
    assert parsed.reason is None
    assert [(s.qualified_name, s.kind) for s in parsed.symbols][-4:] == [
        ("nested::Item::f", "method"), ("nested::Item::f", "method"),
        ("nested::Item::work", "method"), ("nested::Item::work::inner", "function"),
    ]


# 功能：验证 C++ typedef 中的具名类型不被错误放进别名作用域。
# 设计：同一声明同时产生别名、类型和方法，检查三者限定名，防止错误导航到 Alias::Named。
def test_cpp_typedef_does_not_create_scope(tmp_path: Path) -> None:
    path = tmp_path / "sample.hpp"
    path.write_text("namespace app {\n"
                    "typedef struct Named { int get() { return 1; } } Alias;\n}\n", encoding="utf-8")
    parsed = parse_source(path, ReadBudget())
    assert parsed.reason is None
    assert [(s.qualified_name, s.kind) for s in parsed.symbols] == [
        ("app::Alias", "type"), ("app::Named", "class"), ("app::Named::get", "method"),
    ]


@pytest.mark.parametrize("suffix,source", [
    (".cpp", "int answer() { return 42; }\n"),
    (".java", "class Answer { int answer() { return 42; } }\n"),
    (".rs", "fn answer() -> i32 { 42 }\n"),
    (".go", "package main\nfunc answer() int { return 42 }\n"),
])
# 功能：验证新增语言仍经工作区外单次审批，语法适配不能绕过文件访问边界。
# 设计：调用真实权限入口并自动批准两次，检查每次均出现审批且结果包含目标定义。
async def test_new_language_external_approval(tmp_path, suffix, source):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / ("answer" + suffix)
    path.write_text(source, encoding="utf-8")
    permissions, bus = PermissionManager(), EventBus()
    requests = []

    # 核对后端实际解析路径，并只授予本次读取权限。
    async def allow_once(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            assert event.resolved_path == str(path)
            assert event.allowed_decisions == ["allow_once", "deny_once"]
            permissions.respond(event.request_id, "allow_once", session_id=event.session_id,
                                run_id=event.run_id)

    bus.subscribe(allow_once)
    for _ in range(2):
        result = await execute(GetFileOutlineTool(workspace), {"path": str(path)},
                               manager=permissions, bus=bus)
        assert not result.is_error
        assert any(s["name"] == "answer" for s in json.loads(result.content)["symbols"])
    assert len(requests) == 2
