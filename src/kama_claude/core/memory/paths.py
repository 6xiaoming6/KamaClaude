from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from kama_claude.core.memory.model import MemoryError


# 项目记忆归属使用 Git 根，工具执行仍使用原 workspace，不改变 cwd。
def project_root(workspace: Path) -> Path:
    workspace = workspace.resolve(strict=True)
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", timeout=2, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0:
            root = Path(result.stdout.strip()).resolve(strict=True)
            if root.is_dir() and workspace.is_relative_to(root):
                return root
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return workspace


# 同名目录使用不同哈希；Windows 路径按系统大小写规则规范化。
def project_id(root: Path) -> str:
    canonical = os.path.normcase(str(root.resolve()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(canonical).name)[:40] or "project"
    return f"{name}-{digest}"


# 内部记忆路径不能经过链接或 junction，避免专用工具越出绑定的存储目录。
def checked_path(path: Path, home: Path) -> Path:
    try:
        relative = path.relative_to(home)
        current = home
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise MemoryError(f"memory links are not supported: {current}", "permission_denied")
        if not path.resolve().is_relative_to(home):
            raise MemoryError("memory path escaped its storage root", "permission_denied")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, MemoryError):
            raise
        raise MemoryError(f"cannot resolve memory path: {path}", "permission_denied") from exc
    return path


# 普通文件工具保护用户规则及全局自动文件，可信记忆入口不调用此函数。
def guard_file_write(target: Path, workspace: Path) -> None:
    home = (Path.home() / ".kama").resolve()
    protected = [home / "AGENTS.md", home / "AGENTS.auto.md", project_root(workspace) / "AGENTS.md"]
    canonical = os.path.normcase(str(target.resolve()))
    if os.path.normcase(target.name) == os.path.normcase("AGENTS.md") or any(
        canonical == os.path.normcase(str(path.resolve())) for path in protected
    ):
        raise PermissionError("protected instruction file; user rules are read-only, "
                              "use memory_write only for Agent-maintained memory")
