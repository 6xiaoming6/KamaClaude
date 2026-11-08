from __future__ import annotations

from dataclasses import replace

from tree_sitter import Node

from kama_claude.core.tools.syntax.common import (
    Definition,
    SymbolKind,
    named_definition,
    node_text,
    qualify,
    type_name,
)

_KINDS: dict[str, SymbolKind] = {
    "struct_item": "class", "union_item": "class", "enum_item": "enum",
    "trait_item": "interface", "type_item": "type",
    "function_item": "function", "function_signature_item": "function",
}


# impl 和 trait 内的关联函数归属类型，连续属性同时纳入读取范围。
def definition(node: Node, source: bytes, owner: str) -> Definition | None:
    kind = _KINDS.get(node.type)
    if kind is None:
        return None
    if kind == "function" and owner in {"class", "interface"}:
        kind = "method"
    result = named_definition(node, kind, source)
    if result is not None:
        outer = node
        previous = node.prev_named_sibling
        while previous is not None and previous.type == "attribute_item":
            outer = previous
            previous = previous.prev_named_sibling
        if outer != node:
            result = replace(result, outer=outer)
    return result


# 内联模块只增加词法前缀；impl 不伪造定义，使用被实现类型限定子方法。
def child_scope(node: Node, source: bytes, scope: str, owner: str) -> tuple[str, str]:
    if node.type == "mod_item" and node.child_by_field_name("body") is not None:
        name = node.child_by_field_name("name")
        if name is not None:
            return qualify(scope, node_text(name, source), "::"), "module"
    if node.type == "impl_item":
        target = type_name(node.child_by_field_name("type"), source)
        if target:
            return qualify(scope, target, "::"), "class"
    return scope, owner
