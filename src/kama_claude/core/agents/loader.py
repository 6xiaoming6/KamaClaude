from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] | None = None
    model: str = ""


class AgentProfileError(ValueError):
    pass


class _ProfileConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] | None = None
    model: str = ""


# 项目本地 > 用户全局 > 内建；目录和实际派生使用同一份配置。
class AgentProfileLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 项目配置固定从本轮工作区查找，不随运行期间 cwd 改变
    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = (workspace or Path.cwd()).resolve()

    # 指定角色缺失或损坏都明确报错，不能退回无限制工具集合
    def load(self, name: str) -> AgentProfile:
        if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
            raise AgentProfileError(f"invalid subagent type: {name}")
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return self._parse(path, name)
                except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
                    raise AgentProfileError(f"invalid agent profile {name}: {path}: {exc}") from exc
        raise AgentProfileError(f"unknown subagent type: {name}")

    # 仅扫描角色元信息；损坏角色仍列入目录，并在选择时明确拒绝。
    def catalog(self) -> tuple[dict[str, AgentProfile], dict[str, str]]:
        directories = [
            self._BUILTIN_DIR, Path("~/.kama/agents").expanduser(),
            self._workspace / ".kama" / "agents",
        ]
        names = {path.stem for directory in directories for path in directory.glob("*.toml")}
        profiles: dict[str, AgentProfile] = {}
        errors: dict[str, str] = {}
        for name in sorted(names):
            try:
                profiles[name] = self.load(name)
            except AgentProfileError as exc:
                errors[name] = str(exc)
        return profiles, errors

    # 返回 [项目本地, 用户全局, 内建] 路径；load() 返回第一个存在的，项目本地优先级最高
    def _search_paths(self, name: str) -> list[Path]:
        builtin = self._BUILTIN_DIR / f"{name}.toml"
        global_ = Path("~/.kama/agents").expanduser() / f"{name}.toml"
        local = self._workspace / ".kama" / "agents" / f"{name}.toml"
        return [local, global_, builtin]

    # 解析 TOML 角色配置文件
    def _parse(self, path: Path, name: str) -> AgentProfile:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        agent = _ProfileConfig.model_validate(data["agent"])
        return AgentProfile(
            name=name,
            description=agent.description,
            system_prompt=agent.system_prompt.strip(),
            allowed_tools=agent.allowed_tools,
            model=agent.model,
        )
