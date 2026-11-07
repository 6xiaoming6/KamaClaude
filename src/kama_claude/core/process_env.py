from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

# 保留系统运行与路径定位所需变量，不继承模型密钥、代理凭据和任意启动钩子。
_BASE_NAMES = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
    "VIRTUAL_ENV", "PYTHONIOENCODING", "PYTHONUTF8",
})


def build_process_env(
    *, forward_env: Sequence[str] = (), env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    requested = {name.upper() if os.name == "nt" else name for name in forward_env}
    child = {
        name: value for name, value in os.environ.items()
        if name.upper() in _BASE_NAMES
        or (name.upper() if os.name == "nt" else name) in requested
    }
    # Python 子进程默认统一 UTF-8，避免 Windows 管道输出使用本地代码页。
    for name, value in {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}.items():
        if not any(key.upper() == name for key in child):
            child[name] = value
    # 显式传值优先；Windows 环境变量不区分大小写。
    for name, value in (env or {}).items():
        if os.name == "nt":
            child = {key: val for key, val in child.items() if key.upper() != name.upper()}
        child[name] = value
    return child
