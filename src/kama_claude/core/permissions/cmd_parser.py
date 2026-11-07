from __future__ import annotations

import re

_OPERATORS = re.compile(r"&&|\|\|")
_LITERAL = re.compile(r"[\w./:=,+\\\- \t]+", re.ASCII)
_CONTROL_WORDS = {"if", "for", "call", "rem", "goto"}


# cmd 只识别无引号、无展开的字面量及 &&、||，不能借用 Bash AST 解释 Windows 语法。
def parse_commands(command: str) -> list[list[str]] | None:
    commands: list[list[str]] = []
    for segment in _OPERATORS.split(command):
        if not segment.strip(" \t") or _LITERAL.fullmatch(segment) is None:
            return None
        argv = segment.split()
        if argv[0].lower() in _CONTROL_WORDS or "=" in argv[0] or argv[0].startswith(":"):
            return None
        commands.append(argv)
    return commands or None


# 不完整脚本中的字面量前缀仅用于禁止检查，绝不能用于自动放行。
def literal_command_prefixes(command: str) -> list[list[str]]:
    prefixes: list[list[str]] = []
    for segment in _OPERATORS.split(command):
        match = _LITERAL.match(segment.lstrip(" \t"))
        if match is not None and match.group().strip():
            prefixes.append(match.group().split())
    return prefixes
