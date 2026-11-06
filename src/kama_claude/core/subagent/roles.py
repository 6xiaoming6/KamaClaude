from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from kama_claude.core.agents.loader import AgentProfile, AgentProfileError, AgentProfileLoader
from kama_claude.core.compact.estimator import estimate_bytes

# 后台只允许已知的只读工具；Shell、写记忆、继续派生和未知工具均不自动视为只读。
BACKGROUND_TOOLS = frozenset({
    "read_file", "list_dir", "find_files", "search_text", "memory_read",
    "task_get", "task_list", "load_skill", "agent_result", "finish",
})


def effective_tools(profile: AgentProfile, parent: set[str] | None) -> set[str] | None:
    if profile.allowed_tools is None:
        return parent
    requested = set(profile.allowed_tools)
    return requested if parent is None else requested & parent


class RoleCatalog:
    # 一个工具实例持有本轮角色快照；定义变化会改变描述指纹，恢复校验不会静默放宽。
    def __init__(self, workspace: Path) -> None:
        self._profiles, self._errors = AgentProfileLoader(workspace).catalog()
        lines = []
        for name in sorted(self._profiles.keys() | self._errors.keys()):
            if name in self._errors:
                lines.append(f"- {name}: unavailable (invalid role configuration)")
                continue
            profile = self._profiles[name]
            encoded = json.dumps(asdict(profile), ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
            lines.append(f"- {name}: {profile.description or name} [definition={digest}]")
        self.description = "\n".join(lines)
        if estimate_bytes(self.description) > 2_048:
            raise AgentProfileError("agent catalog exceeds 2048 estimated tokens; shorten roles")

    def get(self, name: str) -> AgentProfile:
        if name in self._errors:
            raise AgentProfileError(self._errors[name])
        if name not in self._profiles:
            raise AgentProfileError(f"unknown subagent type: {name}")
        return self._profiles[name]
