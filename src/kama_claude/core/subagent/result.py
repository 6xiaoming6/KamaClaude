from __future__ import annotations

from dataclasses import dataclass

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.tools.base import ToolResult


@dataclass(frozen=True)
class SubagentResult:
    run_id: str
    session_id: str
    parent_run_id: str
    status: str
    reason: str | None
    text: str
    actual_tokens: int
    estimated_tokens: int
    elapsed_seconds: float
    usage_complete: bool
    outcome_unknown: bool

    @classmethod
    # 只复制终态与结果，不保留消息历史、Task 和整份 Context
    def from_run(cls, run: RunCheckpoint, session_id: str, parent_run_id: str) -> SubagentResult:
        unknown = any(
            tool.status in ("executing", "unknown")
            or bool(tool.result and tool.result.get("outcome_unknown"))
            for tool in run.tools
        )
        return cls(
            run.run_id, session_id, parent_run_id, run.status, run.reason, run.result,
            run.actual_tokens, run.estimated_tokens, run.elapsed_seconds, run.usage_complete,
            unknown,
        )

    # 前后台共用结果转换，任务正常返回也必须检查业务状态
    def as_tool_result(self) -> ToolResult:
        if self.status == "success" and not self.outcome_unknown:
            return ToolResult(content=self.text or "Subagent completed with no text output.")
        description = f"Subagent {self.status} (reason={self.reason or 'unknown'})."
        return ToolResult(
            content=description + (f"\n{self.text}" if self.text else ""),
            is_error=True, error_type="subagent_failed", outcome_unknown=self.outcome_unknown,
        )
