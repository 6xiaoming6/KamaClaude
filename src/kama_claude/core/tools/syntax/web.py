from __future__ import annotations

from dataclasses import replace

from tree_sitter import Node

from kama_claude.core.tools.syntax.common import (
    Definition,
    SymbolKind,
    named_definition,
    node_text,
)

_KINDS: dict[str, SymbolKind] = {
    "class_definition": "class", "class_declaration": "class",
    "abstract_class_declaration": "class", "function_definition": "function",
    "function_declaration": "function", "generator_function_declaration": "function",
    "method_definition": "method", "method_signature": "method",
    "abstract_method_signature": "method", "interface_declaration": "interface",
    "type_alias_declaration": "type", "enum_declaration": "enum",
}


# 保留 Python 与 JS/TS 原有规则，包括装饰器、导出和具名箭头函数。
def definition(node: Node, source: bytes, owner: str) -> Definition | None:
    kind = _KINDS.get(node.type)
    if kind is not None:
        if node.type == "function_definition" and owner == "class":
            kind = "method"
        result = named_definition(node, kind, source)
        if result is not None and node.parent is not None:
            if node.parent.type in {"decorated_definition", "export_statement"}:
                result = replace(result, outer=node.parent)
        return result
    if node.type == "variable_declarator":
        name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if (name is not None and name.type == "identifier" and value is not None
                and value.type in {"arrow_function", "function_expression", "generator_function"}):
            return Definition(node_text(name, source), "function", node,
                              value.child_by_field_name("body"),
                              node.parent if node.parent is not None
                              and node.parent.type == "export_statement" else None)
    return None
