from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from kama_claude.core.skills.context import render_skill
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.skills.model import NAME_PATTERN, SkillError
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.checkpoint import RunCheckpoint


class LoadSkillParams(ToolParams):
    name: str = Field(min_length=1, max_length=64, pattern=NAME_PATTERN)


class LoadSkillTool(BaseTool):
    name = "load_skill"
    retry_safe = True
    params_model = LoadSkillParams
    input_schema = LoadSkillParams.model_json_schema()
    description = (
        "Load a skill's complete workflow by name; available names are listed in the system "
        "prompt. One skill per Run. Loading never grants permissions or executes scripts. "
        "Keep the task target in the user message."
    )

    # 仅绑定当前 Run 和加载器；工具描述不再携带 Skill 目录。
    def __init__(self, run: RunCheckpoint, loader: SkillLoader | None = None) -> None:
        self._run = run
        self._loader = loader or SkillLoader()

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = LoadSkillParams.model_validate(params)
        if self._run.active_skill is not None:
            if self._run.active_skill.name == p.name:
                return ToolResult(f"Skill {p.name} is already loaded for this Run.")
            return ToolResult(
                f"Skill {self._run.active_skill.name} is active; select {p.name} in a new Run.",
                is_error=True, error_type="invalid_operation",
            )
        if p.name not in {entry.name for entry in self._run.skill_catalog or []}:
            return ToolResult(f"Unknown skill: {p.name}", is_error=True, error_type="not_found")
        try:
            snapshot = self._loader.load(p.name)
        except SkillError as exc:
            return ToolResult(str(exc), is_error=True, error_type="configuration_error")
        self._run.active_skill = snapshot
        return ToolResult(render_skill(snapshot), preserve_in_context=True)
