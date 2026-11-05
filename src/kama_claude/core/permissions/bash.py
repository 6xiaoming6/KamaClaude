from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kama_claude.core.permissions import bash_parser, cmd_parser

type CommandDecision = Literal["allow", "ask", "deny"]
_SHELLS = {
    "bash", "sh", "dash", "zsh", "ksh", "fish", "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe",
}


@dataclass(frozen=True)
class CommandRule:
    pattern: tuple[str, ...]
    decision: CommandDecision

    # 规则由后端构造，拒绝空前缀与非法决定，避免意外匹配全部命令。
    def __post_init__(self) -> None:
        if (
            not isinstance(self.pattern, tuple) or not self.pattern
            or not all(isinstance(word, str) for word in self.pattern) or not self.pattern[0]
        ):
            raise ValueError("command rule requires a non-empty argv prefix")
        if self.decision not in {"allow", "ask", "deny"}:
            raise ValueError("invalid command rule decision")

    # 逐个参数匹配前缀，字符串中出现同样文字不算命中。
    def matches(self, argv: list[str]) -> bool:
        return argv[:len(self.pattern)] == list(self.pattern)


# 按实际平台解析，再统一检查命令与参数；复杂脚本的静态线索只用于禁止。
def command_decision(
    command: str, *, rules: Sequence[CommandRule], platform: str | None = None,
) -> tuple[CommandDecision, str]:
    if (platform or os.name) == "nt":
        commands = cmd_parser.parse_commands(command)
        prefixes = commands or []
        parser_name = "cmd"
    else:
        root = bash_parser.parse_script(command)
        commands = bash_parser.parse_commands(root) if root is not None else None
        prefixes = bash_parser.literal_command_prefixes(root) if root is not None else []
        parser_name = "Bash"
    if any(rule.decision == "deny" and rule.matches(argv) for rule in rules for argv in prefixes):
        return "deny", "命令及参数命中禁止规则"
    if commands is None:
        return "ask", f"包含动态展开、无效或未支持的 {parser_name} 语法，需确认完整命令"
    # 首版不自动展开嵌套 Shell，避免批准外壳就允许任意内层脚本。
    if any(argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower() in _SHELLS for argv in commands):
        return "ask", "嵌套 Shell 调用需要单次确认"

    decision: CommandDecision = "allow"
    for argv in commands:
        matched = [rule.decision for rule in rules if rule.matches(argv)]
        if "deny" in matched:
            return "deny", "命令及参数命中禁止规则"
        if not matched or "ask" in matched:
            decision = "ask"
    if decision == "allow":
        return "allow", "所有命令及参数均匹配允许规则"
    return "ask", "存在未授权或要求询问的命令及参数"
