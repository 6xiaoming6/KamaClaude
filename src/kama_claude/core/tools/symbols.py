from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_FILE_BYTES, ReadBudget, check_target

type SymbolKind = Literal["class", "function", "method", "interface", "type", "enum"]
EXTENSIONS = frozenset({".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
_KINDS: dict[str, SymbolKind] = {
    "class_definition": "class", "class_declaration": "class",
    "abstract_class_declaration": "class", "function_definition": "function",
    "function_declaration": "function", "generator_function_declaration": "function",
    "method_definition": "method", "method_signature": "method",
    "abstract_method_signature": "method", "interface_declaration": "interface",
    "type_alias_declaration": "type", "enum_declaration": "enum",
}


@dataclass(frozen=True)
class Symbol:
    name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    signature: str

    def record(self, path: str) -> dict[str, object]:
        return {
            "path": path, "name": self.name, "qualified_name": self.qualified_name,
            "kind": self.kind, "start_line": self.start_line, "end_line": self.end_line,
            "signature": self.signature,
        }

    def matches(self, name: str, partial: bool = False) -> bool:
        values = (self.name, self.qualified_name)
        return any(name in value for value in values) if partial else name in values


@dataclass
class SourceFile:
    text: str
    digest: str
    symbols: list[Symbol]
    reason: str | None


# Parser 属于一次调用，不在线程之间共享可变解析器或过期语法树。
def _parser(suffix: str) -> Parser:
    if suffix == ".py":
        language = Language(tree_sitter_python.language())
    elif suffix in {".ts", ".mts", ".cts"}:
        language = Language(tree_sitter_typescript.language_typescript())
    elif suffix == ".tsx":
        language = Language(tree_sitter_typescript.language_tsx())
    else:
        language = Language(tree_sitter_javascript.language())
    return Parser(language)


def _text(raw: bytes, node: Node) -> str:
    return raw[node.start_byte:node.end_byte].decode("utf-8")


def _definition(node: Node) -> tuple[Node, SymbolKind] | None:
    kind = _KINDS.get(node.type)
    if kind is not None:
        name = node.child_by_field_name("name")
        if name is not None:
            return name, kind
    if node.type == "variable_declarator":
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if (name is not None and name.type == "identifier" and value is not None
                and value.type in {"arrow_function", "function_expression", "generator_function"}):
            return name, "function"
    return None


def _symbol(node: Node, name: Node, kind: SymbolKind, scope: str, raw: bytes) -> Symbol:
    # 装饰器、export 属于定义的源码范围；签名只取声明，正文不混入大纲。
    outer = node
    if node.parent is not None and node.parent.type in {"decorated_definition", "export_statement"}:
        outer = node.parent
    body = node.child_by_field_name("body")
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        body = value.child_by_field_name("body") if value is not None else None
    stop = body.start_byte if body is not None else node.end_byte
    signature = " ".join(raw[node.start_byte:min(stop, node.start_byte + 1200)]
                         .decode("utf-8", errors="ignore").split())
    if len(signature) > 300 or stop - node.start_byte > 1200:
        signature = signature[:300] + " …"
    identifier = _text(raw, name)
    end_line = outer.end_point.row + (1 if outer.end_point.column else 0)
    return Symbol(identifier, f"{scope}.{identifier}" if scope else identifier, kind,
                  outer.start_point.row + 1, max(outer.start_point.row + 1, end_line), signature)


def parse_source(path: Path, budget: ReadBudget) -> SourceFile:
    check_target(path)
    if not path.is_file():
        if not path.exists():
            raise ToolError(f"file not found: {path}", "not_found")
        raise ToolError("path must be a concrete source file")
    if path.suffix.lower() not in EXTENSIONS:
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
    tree = _parser(path.suffix.lower()).parse(source)
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
        definition = _definition(node)
        if definition is not None:
            name, kind = definition
            if node.type == "function_definition" and owner == "class":
                kind = "method"
            symbol = _symbol(node, name, kind, scope, source)
            symbols.append(symbol)
            scope, owner = symbol.qualified_name, kind
        stack.extend((child, scope, owner) for child in reversed(node.named_children))
    return SourceFile(text, hashlib.sha256(raw).hexdigest(), symbols, reason)
