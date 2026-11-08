from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from tree_sitter import Parser

from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_FILE_BYTES, ReadBudget, check_target
from kama_claude.core.tools.syntax import EXTENSIONS, normalized_suffix, syntax_for
from kama_claude.core.tools.syntax.common import Definition, SymbolKind, qualify


@dataclass(frozen=True)
class Symbol:
    name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    signature: str

    # 统一工具输出字段，语言差异只体现在名称、签名及源码位置中。
    def record(self, path: str) -> dict[str, object]:
        return {
            "path": path, "name": self.name, "qualified_name": self.qualified_name,
            "kind": self.kind, "start_line": self.start_line, "end_line": self.end_line,
            "signature": self.signature,
        }

    # 简单名和工具返回的限定名均可匹配，不推测导入别名或运行时类型。
    def matches(self, name: str, partial: bool = False) -> bool:
        values = (self.name, self.qualified_name)
        return any(name in value for value in values) if partial else name in values


@dataclass
class SourceFile:
    text: str
    digest: str
    symbols: list[Symbol]
    reason: str | None


# 源码范围包含装饰器或属性，签名仅截取声明，不混入函数、类型的正文。
def _symbol(definition: Definition, scope: str, separator: str, raw: bytes) -> Symbol:
    node, body = definition.node, definition.body
    outer = definition.outer or node
    stop = body.start_byte if body is not None else node.end_byte
    signature = " ".join(raw[node.start_byte:min(stop, node.start_byte + 1200)]
                         .decode("utf-8", errors="ignore").split())
    if len(signature) > 300 or stop - node.start_byte > 1200:
        signature = signature[:300] + " …"
    end = outer if outer.end_byte > node.end_byte else node
    end_line = end.end_point.row + (1 if end.end_point.column else 0)
    return Symbol(
        definition.name, qualify(scope, definition.qualified_name or definition.name, separator),
        definition.kind, outer.start_point.row + 1,
        max(outer.start_point.row + 1, end_line), signature,
    )


# 统一校验和有界遍历，语言适配器只识别声明及词法作用域。
def parse_source(path: Path, budget: ReadBudget) -> SourceFile:
    check_target(path)
    if not path.is_file():
        if not path.exists():
            raise ToolError(f"file not found: {path}", "not_found")
        raise ToolError("path must be a concrete source file")
    suffix = normalized_suffix(path.suffix)
    if suffix not in EXTENSIONS:
        raise ToolError("unsupported language; use search_text and read_file", "invalid_operation")
    if not budget.check():
        raise ToolError(budget.reason, "resource_limit")
    with path.open("rb") as stream:
        before = path.stat()
        raw = stream.read(min(MAX_FILE_BYTES, budget.max_bytes - budget.consumed) + 1)
        after = path.stat()
    check_target(path)
    if (before.st_mtime_ns, before.st_size, before.st_ino) != (
        after.st_mtime_ns, after.st_size, after.st_ino,
    ):
        raise ToolError("file changed while reading; retry the query", "conflict")
    if not budget.check(len(raw)):
        raise ToolError(budget.reason, "resource_limit")
    if len(raw) > MAX_FILE_BYTES:
        raise ToolError("source exceeds the 1 MiB parsing limit; use search_text/read_file",
                        "resource_limit")
    if b"\0" in raw:
        raise ToolError("binary source is not supported")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ToolError("source is not valid UTF-8") from exc
    # Tree-sitter 的字节偏移必须对应实际送入解析器的 UTF-8 字节。
    source = text.encode("utf-8")
    syntax = syntax_for(suffix)
    tree = Parser(syntax.language).parse(source)
    symbols: list[Symbol] = []
    reason = "parse_error" if tree.root_node.has_error else None
    stack = [(tree.root_node, "", "")]
    visited = 0
    while stack:
        node, scope, owner = stack.pop()
        visited += 1
        if visited > 50_000 or not budget.check():
            reason = budget.reason or "syntax_nodes_limit"
            break
        definition = syntax.definition(node, source, owner)
        if definition is not None:
            symbol = _symbol(definition, scope, syntax.separator, source)
            symbols.append(symbol)
            if definition.introduces_scope:
                scope, owner = symbol.qualified_name, definition.kind
        if syntax.child_scope is not None:
            scope, owner = syntax.child_scope(node, source, scope, owner)
        stack.extend((child, scope, owner) for child in reversed(node.named_children))
    # 同文件已声明的类型可确定类外方法归属；不推测未读取头文件中的类型。
    classes = {symbol.qualified_name for symbol in symbols if symbol.kind == "class"}
    symbols = [replace(symbol, kind="method")
               if symbol.kind == "function"
               and symbol.qualified_name.rpartition(syntax.separator)[0] in classes
               else symbol for symbol in symbols]
    return SourceFile(text, hashlib.sha256(raw).hexdigest(), symbols, reason)
