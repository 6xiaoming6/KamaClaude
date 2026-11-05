from __future__ import annotations

import json
from typing import Any


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "find_files": ("pattern", "path"),
        "search_text": ("pattern", "path"),
        "edit_file": ("path",),
        "bash": ("command",),
        "memory_read": ("scope", "key", "query"),
        "memory_write": ("scope", "key", "action"),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


def render_context_bar(pct: float) -> str:
    filled = int(pct * 20)
    bar = "█" * filled + "░" * (20 - filled)
    label = f"ctx:{pct * 100:.1f}%"
    if pct >= 0.85:
        color = "bold red"
    elif pct >= 0.70:
        color = "yellow"
    else:
        color = "dim"
    return f"[{color}]{label} {bar}[/{color}]"
