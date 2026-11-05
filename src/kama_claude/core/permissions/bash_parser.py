from __future__ import annotations

from collections.abc import Iterator

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_bash.language())
_ALLOWED_NODES = {
    "program", "list", "pipeline", "command", "command_name", "word", "number",
    "string", "string_content", "raw_string", "concatenation",
}
_ALLOWED_TOKENS = {"&&", "||", ";", "|", '"', "'"}
_EXPANSIONS = frozenset("{}*?[]\\~^#$`")


# 只构建 Bash 语法树，不执行脚本；语法错误不能用于自动放行。
def parse_script(command: str) -> Node | None:
    try:
        source = command.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if b"\0" in source:
        return None
    root = Parser(_LANGUAGE).parse(source).root_node
    return None if root.has_error else root


# 按源码顺序遍历节点，避免嵌套脚本导致递归遍历栈溢出。
def _walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


# 提取可确定的字面量参数，保留空参数和 UTF-8 内容，不求值变量或展开。
def _literal(node: Node) -> str | None:
    if node.text is None:
        return None
    text = node.text.decode("utf-8")
    if node.type in {"word", "number"}:
        if node.named_child_count or text.startswith("=") or any(c in _EXPANSIONS for c in text):
            return None
        return text
    if node.type == "raw_string":
        return text[1:-1] if text.startswith("'") and text.endswith("'") else None
    if node.type == "string":
        if any(child.type != "string_content" for child in node.named_children):
            return None
        if not (text.startswith('"') and text.endswith('"')):
            return None
        content = text[1:-1]
        if any(a == "\\" and b in '$`"\\\n' for a, b in zip(content, content[1:])):
            return None
        return content
    if node.type == "concatenation":
        parts = [_literal(child) for child in node.named_children]
        if not parts or any(part is None for part in parts):
            return None
        return "".join(part for part in parts if part is not None)
    return None


# 验证整棵树只包含受支持语法，再返回每条简单命令的参数列表。
def parse_commands(root: Node) -> list[list[str]] | None:
    commands: list[list[str]] = []
    for node in _walk(root):
        if node.is_named:
            if node.type not in _ALLOWED_NODES:
                return None
        elif node.type not in _ALLOWED_TOKENS and node.type.strip():
            return None
        if node.type in {"word", "number"} and _literal(node) is None:
            return None
        if node.type == "command":
            argv: list[str] = []
            for child in node.named_children:
                if child.type == "command_name":
                    name = child.named_child(0)
                    if name is None or name.type != "word":
                        return None
                    child = name
                value = _literal(child)
                if value is None:
                    return None
                argv.append(value)
            if not argv or not argv[0]:
                return None
            commands.append(argv)
    return commands or None


# 复杂脚本只提取连续可确定的命令前缀，用于禁止检查，绝不能据此允许。
def literal_command_prefixes(root: Node) -> list[list[str]]:
    commands: list[list[str]] = []
    for node in _walk(root):
        if node.type != "command":
            continue
        name = node.child_by_field_name("name")
        if name is None or name.named_child_count != 1:
            continue
        executable = _literal(name.named_children[0])
        if not executable:
            continue
        argv = [executable]
        for child in node.named_children:
            if child.end_byte <= name.end_byte:
                continue
            value = _literal(child)
            if value is None:
                break
            argv.append(value)
        commands.append(argv)
    return commands
