from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tree_sitter import Node

type SymbolKind = Literal["class", "function", "method", "interface", "type", "enum"]


@dataclass(frozen=True)
class Definition:
    name: str
    kind: SymbolKind
    node: Node
    body: Node | None = None
    outer: Node | None = None
    qualified_name: str | None = None
    introduces_scope: bool = True  # C++ typedef/using 是别名，不拥有内部具名声明。


# 按 Tree-sitter 的字节范围读取名称，不能把字节偏移当作字符串下标。
def node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


# 拼接词法作用域；已限定或显式全局的 C++ 名称不重复添加命名空间。
def qualify(scope: str, name: str, separator: str = ".") -> str:
    if name.startswith("::"):
        return name[2:]
    if not scope or name.startswith(scope + separator):
        return name
    return scope + separator + name


# 为具名声明生成统一记录；无名回调和匿名类型不编造符号名称。
def named_definition(node: Node, kind: SymbolKind, source: bytes) -> Definition | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    return Definition(node_text(name, source), kind, node, node.child_by_field_name("body"))


# 去掉接收者或实现类型外层的指针、引用和泛型，只保留具名类型。
def type_name(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type in {"type_identifier", "identifier", "primitive_type"}:
        return node_text(node, source)
    child = node.child_by_field_name("type") or node.child_by_field_name("name")
    if child is not None:
        return type_name(child, source)
    if node.type in {"pointer_type", "reference_type"} and node.named_children:
        return type_name(node.named_children[-1], source)
    return ""
