from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kama_claude.core.permissions import bash_parser, cmd_parser
from kama_claude.core.permissions.mode import PermissionMode

type CommandDecision = Literal["allow", "ask", "deny"]
_SHELLS = {
    "bash", "sh", "dash", "zsh", "ksh", "fish", "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe",
}
_HIGH_RISK_COMMANDS = {
    "del", "erase", "rd", "rmdir", "rm", "shred", "unlink",
    "format", "diskpart", "mkfs", "dd", "chmod", "chown", "icacls", "cacls",
    "taskkill", "kill", "killall", "pkill", "shutdown", "reboot",
    "reg", "regedit", "setx", "sc", "net", "sudo", "su",
    "wscript", "cscript", "mshta", "rundll32", "regsvr32",
}
_INTERPRETERS = {
    "python", "python3", "python3.12", "py", "node", "nodejs", "ruby", "perl", "php",
}
_SCRIPT_SUFFIXES = (".bat", ".cmd", ".ps1", ".sh", ".py", ".js", ".rb", ".pl")


# 只做可解释的保守分类，不执行或猜测脚本内容；外层解析成功不代表脚本安全。
def _requires_confirmation(argv: list[str]) -> bool:
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    if name in _HIGH_RISK_COMMANDS or name.startswith("mkfs."):
        return True
    if name in _SHELLS or name.endswith(_SCRIPT_SUFFIXES):
        return True
    if name in _INTERPRETERS:
        return argv[1:] not in (["--version"], ["-V"])
    if name != "git":
        return False

    # 跳过 Git 全局选项及其值，不能把 `git -C repo reset --hard` 当成普通命令。
    args = argv[1:]
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in {
            "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
        } else 1
    if index >= len(args):
        return False
    operation, flags = args[index], args[index + 1:]
    if operation in {
        "reset", "clean", "restore", "checkout", "switch", "rebase", "merge",
        "cherry-pick", "revert", "push", "prune", "gc",
    }:
        return True
    return operation in {"branch", "tag"} and any(f in {"-d", "-D", "--delete"} for f in flags)


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
    def matches(self, argv: list[str], *, platform: str | None = None) -> bool:
        if not argv:
            return False
        expected, actual = self.pattern[0], argv[0]
        if (platform or os.name) == "nt":
            expected, actual = expected.casefold(), actual.casefold()
        return actual == expected and argv[1:len(self.pattern)] == list(self.pattern[1:])


# 按实际平台解析，再统一检查命令与参数；复杂脚本的静态线索只用于禁止。
def command_decision(
    command: str, *, rules: Sequence[CommandRule], platform: str | None = None,
    mode: PermissionMode = PermissionMode.ASK,
) -> tuple[CommandDecision, str]:
    if (platform or os.name) == "nt":
        commands = cmd_parser.parse_commands(command)
        prefixes = commands or cmd_parser.literal_command_prefixes(command)
        parser_name = "cmd"
    else:
        root = bash_parser.parse_script(command)
        commands = bash_parser.parse_commands(root) if root is not None else None
        prefixes = bash_parser.literal_command_prefixes(root) if root is not None else []
        parser_name = "Bash"
    if any(
        rule.decision == "deny" and rule.matches(argv, platform=platform)
        for rule in rules for argv in prefixes
    ):
        return "deny", "命令及参数命中禁止规则"
    # full 只跳过询问，不跳过上面的禁止检查。
    if mode == PermissionMode.FULL:
        return "allow", "full 模式：无需审批"
    if commands is None:
        return "ask", f"包含动态展开、无效或未支持的 {parser_name} 语法，需确认完整命令"
    # 首版不自动展开嵌套 Shell，避免批准外壳就允许任意内层脚本。
    if any(argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower() in _SHELLS for argv in commands):
        return "ask", "嵌套 Shell 调用需要单次确认"

    if mode == PermissionMode.APPROVE:
        if any(_requires_confirmation(argv) for argv in commands):
            return "ask", "高危操作或无法分析的脚本需要单次确认"
        if any(
            rule.decision == "ask" and rule.matches(argv, platform=platform)
            for rule in rules for argv in commands
        ):
            return "ask", "命令及参数命中询问规则"
        return "allow", "approve 模式：命令完整解析且未识别到高危操作"

    decision: CommandDecision = "allow"
    for argv in commands:
        matched = [rule.decision for rule in rules if rule.matches(argv, platform=platform)]
        if "deny" in matched:
            return "deny", "命令及参数命中禁止规则"
        if not matched or "ask" in matched:
            decision = "ask"
    if decision == "allow":
        return "allow", "所有命令及参数均匹配允许规则"
    return "ask", "存在未授权或要求询问的命令及参数"
