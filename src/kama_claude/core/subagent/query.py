from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from kama_claude.core.subagent.registry import ActiveSubagent, BackgroundTaskRegistry
from kama_claude.core.subagent.result import PENDING_RESULT
from kama_claude.core.subagent.store import SubagentStoreError
from kama_claude.core.tools.base import BaseTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError

RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class AgentIdsParams(ToolParams):
    run_ids: list[RunId] = Field(
        min_length=1, max_length=8, description="One to eight child Run IDs.",
    )

    @field_validator("run_ids")
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("run_ids must not contain duplicates")
        return value


class AgentResultParams(AgentIdsParams):
    wait_s: float = Field(
        default=10, ge=0, le=30, allow_inf_nan=False,
        description="Wait for any result, at most this many seconds; 0 queries immediately.",
    )


class AgentResultTool(BaseTool):
    name = "agent_result"
    description = (
        "Collect results of one or more subagents from this session. Return completed reports "
        "and still-running IDs; if all are running, wait up to wait_s for any to finish. "
        "Query only remaining IDs next time. Waiting never cancels work. "
        "Completed results remain queryable from disk after cache eviction or Core restart."
    )
    params_model = AgentResultParams
    input_schema = AgentResultParams.model_json_schema()
    retry_safe = True

    def __init__(self, task_registry: BackgroundTaskRegistry, session_id: str) -> None:
        self._task_registry = task_registry
        self._session_id = session_id

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        try:
            entries = await self._task_registry.wait_results(p.run_ids, self._session_id, p.wait_s)
        except SubagentStoreError as exc:
            return ToolResult(str(exc), is_error=True, error_type="storage_error")
        parts: list[str] = []
        collected: list[str] = []
        failed = unknown = missing = False
        for run_id, entry in entries.items():
            if entry is None:
                parts.append(f"run_id={run_id}: Unknown Run ID for this session (no saved result).")
                missing = True
            elif isinstance(entry, ActiveSubagent):
                parts.append(f"run_id={run_id}: {PENDING_RESULT}")
            else:
                result = entry.as_tool_result(max_chars=max(500, 4_000 // len(entries)))
                parts.append(result.content)
                collected.extend(result.collected_subagents)
                failed |= result.is_error
                unknown |= result.outcome_unknown
        return ToolResult(
            "\n\n".join(parts), is_error=failed or missing,
            error_type="not_found" if missing else "subagent_failed" if failed else None,
            outcome_unknown=unknown, collected_subagents=list(dict.fromkeys(collected)),
        )


class AgentCancelTool(BaseTool):
    name = "agent_cancel"
    description = (
        "Request cancellation of this Run's unneeded child tasks and their descendants. "
        "Cancellation can leave unknown side effects. Collect terminal results with agent_result "
        "afterwards; a cancellation request alone is not a completed result."
    )
    params_model = AgentIdsParams
    input_schema = AgentIdsParams.model_json_schema()
    retry_safe = True

    def __init__(
        self, task_registry: BackgroundTaskRegistry, session_id: str, parent_run_id: str,
    ) -> None:
        self._task_registry = task_registry
        self._session_id = session_id
        self._parent_run_id = parent_run_id

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentIdsParams.model_validate(params)
        parts: list[str] = []
        errors: set[str] = set()
        for run_id in p.run_ids:
            try:
                state = await self._task_registry.cancel_child(
                    run_id, self._parent_run_id, self._session_id,
                )
                parts.append(f"run_id={run_id}: {state}; collect with agent_result.")
            except ToolError as exc:
                parts.append(f"run_id={run_id}: {exc}")
                errors.add(exc.error_type)
        error_type = next(iter(errors)) if len(errors) == 1 else "invalid_operation"
        return ToolResult("\n".join(parts), is_error=bool(errors),
                          error_type=error_type if errors else None)
