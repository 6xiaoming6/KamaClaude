from __future__ import annotations

from dataclasses import replace

from tree_sitter import Node

from kama_claude.core.tools.syntax.common import (
    Definition,
    SymbolKind,
    named_definition,
    node_text,
    qualify,
)

_KINDS: dict[str, SymbolKind] = {
    "class_specifier": "class", "struct_specifier": "class", "union_specifier": "class",
    "enum_specifier": "enum", "alias_declaration": "type",
}


# 将具名 C++ 限定声明规范化，泛型实参不改变所属类型的导航名称。
def qualified_name(node: Node, source: bytes) -> str:
    if node.type == "qualified_identifier":
        scope, name = node.child_by_field_name("scope"), node.child_by_field_name("name")
        if name is not None:
            prefix = qualified_name(scope, source) if scope is not None else ""
            return prefix + "::" + qualified_name(name, source)
    if node.type in {"template_type", "template_function"}:
        name = node.child_by_field_name("name")
        if name is not None:
            return qualified_name(name, source)
    return node_text(node, source)


# 找到函数声明器所属的声明，不穿越参数、表达式或其他函数边界。
def function_container(node: Node) -> Node | None:
    container = node.parent
    while container is not None and container.type in {
        "pointer_declarator", "reference_declarator", "attributed_declarator",
    }:
        container = container.parent
    return container if container is not None and container.type in {
        "function_definition", "declaration", "field_declaration",
    } else None


# 提取函数声明器的真实名称，函数指针变量不作为函数定义返回。
def function_definition(node: Node, source: bytes, owner: str) -> Definition | None:
    name = node.child_by_field_name("declarator")
    if name is None or name.type not in {
        "identifier", "field_identifier", "qualified_identifier", "destructor_name",
        "operator_name", "template_function",
    }:
        return None
    container = function_container(node)
    if container is None:
        return None
    qualified = qualified_name(name, source)
    terminal = name
    while terminal.type == "qualified_identifier":
        child = terminal.child_by_field_name("name")
        if child is None:
            return None
        terminal = child
    result = Definition(
        qualified_name(terminal, source), "method" if owner == "class" else "function",
        container, container.child_by_field_name("body"), qualified_name=qualified,
    )
    if container.parent is not None and container.parent.type == "template_declaration":
        result = replace(result, node=container.parent)
    return result


# 转换运算符没有普通名称字段，用参数列表之前的声明构成导航名称。
def conversion_definition(node: Node, container: Node, source: bytes) -> Definition | None:
    declarator = node.child_by_field_name("declarator")
    while declarator is not None and declarator.type != "abstract_function_declarator":
        declarator = declarator.child_by_field_name("declarator")
    if declarator is None:
        return None
    name = " ".join(source[node.start_byte:declarator.start_byte].decode("utf-8").split())
    return Definition(name, "method", container, container.child_by_field_name("body"))


# 类、枚举、别名和函数分别按声明节点提取，不把调用表达式当成定义。
def definition(node: Node, source: bytes, owner: str) -> Definition | None:
    kind = _KINDS.get(node.type)
    if kind is not None:
        result = named_definition(node, kind, source)
        if result is not None and kind == "type":
            result = replace(result, introduces_scope=False)
        if result is not None and node.parent is not None:
            if node.parent.type == "template_declaration":
                result = replace(result, node=node.parent)
        return result
    if node.type == "function_definition":
        declarator = node.child_by_field_name("declarator")
        while declarator is not None and declarator.type in {
            "pointer_declarator", "reference_declarator", "attributed_declarator",
        }:
            declarator = declarator.child_by_field_name("declarator")
        if declarator is not None and declarator.type == "function_declarator":
            return function_definition(declarator, source, owner)
        if declarator is not None and declarator.type == "operator_cast":
            return conversion_definition(declarator, node, source)
    if node.type == "function_declarator":
        container = function_container(node)
        if container is not None and container.type in {"declaration", "field_declaration"}:
            return function_definition(node, source, owner)
    if node.type == "operator_cast":
        container = function_container(node)
        if container is not None and container.type in {"declaration", "field_declaration"}:
            return conversion_definition(node, container, source)
    if node.type == "type_definition":
        name = node.child_by_field_name("declarator")
        if name is not None and name.type in {"identifier", "type_identifier"}:
            return Definition(node_text(name, source), "type", node, introduces_scope=False)
    return None


# namespace 提供限定名前缀，匿名命名空间不编造名称。
def child_scope(node: Node, source: bytes, scope: str, owner: str) -> tuple[str, str]:
    if node.type == "namespace_definition":
        name = node.child_by_field_name("name")
        if name is not None:
            return qualify(scope, node_text(name, source), "::"), "namespace"
    return scope, owner
