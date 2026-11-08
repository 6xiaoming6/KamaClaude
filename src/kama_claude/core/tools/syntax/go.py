from __future__ import annotations

from dataclasses import replace

from tree_sitter import Node

from kama_claude.core.tools.syntax.common import Definition, SymbolKind, named_definition, type_name


# Go 的函数、接收者方法、结构体、接口和别名共用现有结果结构。
def definition(node: Node, source: bytes, owner: str) -> Definition | None:
    if node.type in {"type_spec", "type_alias"}:
        target = node.child_by_field_name("type")
        kind: SymbolKind = (
            "type" if node.type == "type_alias"
            else "interface" if target is not None and target.type == "interface_type"
            else "class" if target is not None and target.type == "struct_type" else "type"
        )
        result = named_definition(node, kind, source)
        if result is not None and target is not None:
            # struct/interface 的成员不属于签名；普通别名保留右侧类型。
            body = next((child for child in target.children
                         if child.type in {"{", "field_declaration_list"}), None)
            result = replace(result, body=body)
            parent = node.parent
            if parent is not None and parent.type == "type_declaration":
                if len(parent.named_children) == 1:
                    result = replace(result, node=parent)
        return result
    if node.type in {"function_declaration", "method_declaration", "method_elem"}:
        result = named_definition(
            node, "function" if node.type == "function_declaration" else "method", source,
        )
        if result is not None and node.type == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            if receiver is not None and receiver.named_children:
                target = receiver.named_children[0].child_by_field_name("type")
                name = type_name(target, source)
                if name:
                    result = replace(result, qualified_name=f"{name}.{result.name}")
        return result
    return None
