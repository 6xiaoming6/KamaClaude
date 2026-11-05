from __future__ import annotations

import pytest

from kama_claude.core.permissions import bash_parser, cmd_parser
from kama_claude.core.permissions.bash import CommandRule, command_decision


# 构建真实 tree-sitter 语法树并检查完整命令，测试不执行任何脚本。
def parse(command: str) -> list[list[str]] | None:
    root = bash_parser.parse_script(command)
    return bash_parser.parse_commands(root) if root is not None else None


# 功能：验证 Bash 引号、参数拼接、中文与操作符按真实参数边界解析
# 设计：直接使用安装的语法库，区分引号中的操作符和真正的命令分隔符
@pytest.mark.parametrize(("command", "expected"), [
    ("git status", [["git", "status"]]),
    ('rg "hello world" src', [["rg", "hello world", "src"]]),
    ("echo '&&'", [["echo", "&&"]]),
    ('echo ""', [["echo", ""]]),
    ('rg -g"*.py" src', [["rg", "-g*.py", "src"]]),
    ("cat '学习资料/项目 说明.md'", [["cat", "学习资料/项目 说明.md"]]),
    ("echo 'a;b|c'", [["echo", "a;b|c"]]),
    ("git status && git diff || echo done", [
        ["git", "status"], ["git", "diff"], ["echo", "done"],
    ]),
    ("rg TODO src | head -n 10; git status", [
        ["rg", "TODO", "src"], ["head", "-n", "10"], ["git", "status"],
    ]),
    ("git status\n git diff", [["git", "status"], ["git", "diff"]]),
    ("echo 'a\\b'", [["echo", "a\\b"]]),
])
def test_bash_literal_commands(command: str, expected: list[list[str]]) -> None:
    assert parse(command) == expected


# 功能：验证动态或不支持的 Bash 语法不能进入自动允许路径
# 设计：覆盖语法树错误、重定向、展开、控制流及 Shell 转义，防止只检查局部子树
@pytest.mark.parametrize("command", [
    "", "  ", "echo 'unclosed", "git status &&", "echo \0bad",
    "echo $HOME", 'echo "$HOME"', "echo $(whoami)", "echo *", "echo ?.txt",
    "echo [ab]", "echo {a,b}", "echo ~", r"echo a\ b", r'echo "a\$b"',
    "echo a > out.txt", "cat < in.txt", "(git status)", "{ git status; }",
    "if true; then git status; fi", "for x in a; do echo x; done",
    "A=value git status", "echo a &", "echo a |& cat", "echo ok # comment",
    "cat <<EOF\nhello\nEOF", "echo <(whoami)", "echo $'hello'",
])
def test_bash_dynamic_syntax_is_not_plain(command: str) -> None:
    assert parse(command) is None


# 功能：验证复杂脚本提取的字面量只用于禁止，并且不跳过动态参数拼出假前缀
# 设计：用真实命令替换、动态目标和动态中间参数区分允许与禁止所需的证明强度
@pytest.mark.parametrize(("command", "expected"), [
    ('rm -rf "$target"', "deny"),
    ("echo $(rm -rf /tmp/test)", "deny"),
    ("if true; then rm -rf /tmp/test; fi", "deny"),
    ("echo 'rm -rf /tmp/test'", "allow"),
    ('git "$action" status', "ask"),
    ('echo "$HOME"', "ask"),
])
def test_bash_partial_facts_only_deny(command: str, expected: str) -> None:
    rules = [
        CommandRule(("echo",), "allow"),
        CommandRule(("git", "status"), "deny"),
        CommandRule(("rm", "-rf"), "deny"),
    ]
    assert command_decision(command, rules=rules, platform="posix")[0] == expected


# 功能：验证 cmd 保留自己的有限语法，Windows 路径不会被 Bash 转义规则修改
# 设计：输入相同反斜线和展开语法，直接比较两个解析入口而不切换真实执行器
def test_cmd_is_not_parsed_as_bash() -> None:
    command = r"type C:\repo\a.txt && echo done"
    assert cmd_parser.parse_commands(command) == [
        ["type", r"C:\repo\a.txt"], ["echo", "done"],
    ]
    assert parse(command) is None


# 功能：验证 cmd 不支持语法全部转为询问，不因相似 Bash 语法自动允许
# 设计：覆盖 Windows 展开、转义、控制流和双引号，规则再宽也不能绕过完整解析
@pytest.mark.parametrize("command", [
    "", "echo a\nwhoami", "echo a\rwhoami", "git status &&", "|| git status",
    "git status && || echo x", "echo %PATH%", "echo !PATH!", "echo ^& whoami",
    "@echo hi", 'echo "a && whoami"', "echo 'hello'", "echo a > file",
    "echo a | whoami", "(echo a)", "echo a & whoami", "if exist file echo a",
    "for %x in (a) do echo %x", "call other.cmd", "rem comment", "echo a; whoami",
    "echo 学习资料", "echo a\u2028whoami", "echo \0bad",
])
def test_cmd_unsupported_syntax_asks(command: str) -> None:
    rules = [CommandRule(("echo",), "allow"), CommandRule(("git",), "allow")]
    assert command_decision(command, rules=rules, platform="nt")[0] == "ask"


# 功能：验证参数前缀匹配与最严格决定在两个解析入口中一致
# 设计：让允许、询问和禁止规则重叠，检查后续段禁止优先于前段询问
@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize(("command", "expected"), [
    ("git status", "allow"),
    ("git    status --short", "allow"),
    ("git status-extra", "ask"),
    ("git status && git diff", "allow"),
    ("git status && python script.py", "ask"),
    ("git push", "ask"),
    ("git push --force", "deny"),
    ("unknown action && git push --force", "deny"),
    ("echo git push --force", "allow"),
])
def test_rule_priority_and_argument_boundaries(platform: str, command: str, expected: str) -> None:
    rules = [
        CommandRule(("git", "status"), "allow"), CommandRule(("git", "diff"), "allow"),
        CommandRule(("git", "push"), "allow"), CommandRule(("git", "push"), "ask"),
        CommandRule(("git", "push", "--force"), "deny"), CommandRule(("echo",), "allow"),
    ]
    assert command_decision(command, rules=rules, platform=platform)[0] == expected


# 功能：验证管道和分号中的每个命令都单独判断，解析成功不等于允许
# 设计：同一合法 Bash 脚本逐步添加规则，只有所有段均授权后才能允许
def test_bash_pipeline_requires_all_commands() -> None:
    rules = [CommandRule(("rg",), "allow")]
    command = "rg TODO src | head -n 10; git status"
    assert command_decision(command, rules=rules, platform="posix")[0] == "ask"
    rules.extend([CommandRule(("head", "-n"), "allow"), CommandRule(("git", "status"), "allow")])
    assert command_decision(command, rules=rules, platform="posix")[0] == "allow"


# 功能：验证空规则默认询问，嵌套 Shell 不会因外壳获准而自动执行内层脚本
# 设计：配置针对外壳的允许规则，仍要求确认，不执行或递归求值内层内容
@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_empty_rules_and_nested_shell(platform: str) -> None:
    assert command_decision("git status", rules=[], platform=platform)[0] == "ask"
    for program in ("bash", "cmd.exe", "powershell.exe"):
        assert command_decision(
            program + " -c whoami", rules=[CommandRule((program,), "allow")], platform=platform,
        )[0] == "ask"


# 功能：验证非法规则在构造时就报错，不引入空前缀全放行
# 设计：故意输入空程序和未知决定，避免运行中才发现错误配置
@pytest.mark.parametrize(("pattern", "decision"), [((), "allow"), (("",), "allow"), (("git",), "bad")])
def test_invalid_command_rules(pattern: tuple[str, ...], decision: str) -> None:
    with pytest.raises(ValueError):
        CommandRule(pattern, decision)  # type: ignore[arg-type]
