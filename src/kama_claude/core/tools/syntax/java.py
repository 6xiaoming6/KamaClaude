from __future__ import annotations

from tree_sitter import Node

from kama_claude.core.tools.syntax.common import Definition, SymbolKind, named_definition

_KINDS: dict[str, SymbolKind] = {
    "class_declaration": "class", "record_declaration": "class",
    "interface_declaration": "interface", "annotation_type_declaration": "interface",
    "enum_declaration": "enum", "method_declaration": "method",
    "constructor_declaration": "method", "compact_constructor_declaration": "method",
    "annotation_type_element_declaration": "method",
}


# Java 的构造器、接口方法、record 和注解成员统一使用已有符号类型。
def definition(node: Node, source: bytes, owner: str) -> Definition | None:
    kind = _KINDS.get(node.type)
    return named_definition(node, kind, source) if kind is not None else None
