from __future__ import annotations

import os
from pathlib import Path

from kama_claude.core.execution.model import ExecutionSnapshot


# 以实际后端说明语法和路径；Windows 宿主上的容器同样使用 Linux Bash。
def execution_instructions(snapshot: ExecutionSnapshot | None, workspace: str) -> str:
    if snapshot is not None and snapshot.backend == "docker":
        return (
            "\n\n## Command execution environment\n"
            "The bash tool runs Linux Bash in a Docker container, not the host shell. "
            f"Host workspace {workspace} is mounted read-write at /workspace. "
            "Use /workspace or workspace-relative paths in commands, never Windows paths or cd /d. "
            "Dedicated file tools still use host workspace paths. Each command starts at "
            "/workspace in a fresh shell; cd and exports do not persist. Writable files and "
            "user-installed dependencies persist only while this Run's container is alive. "
            "Networking is enabled. Root filesystem is read-only; use /tmp, /home/kama or "
            "/workspace for writes. Root .env files are masked except .env.example. "
            "After interruption/resume the container is new; only workspace files are retained. "
            "A host Windows .venv is not a Linux environment; create a separate environment "
            "inside the container if needed."
        )
    shell = "Windows cmd" if os.name == "nt" else "POSIX /bin/sh"
    return (
        f"\n\n## Command execution environment\nCommands run on the host using {shell}, "
        f"starting in {Path(workspace)}. cd and exports do not persist across calls. "
        + ("Windows cmd does not support POSIX heredocs; write complex scripts to a file first."
           if os.name == "nt" else "Use syntax supported by /bin/sh.")
    )
