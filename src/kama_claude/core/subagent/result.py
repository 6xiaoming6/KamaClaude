from __future__ import annotations

from dataclasses import dataclass

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.subagent.report import summarize
from kama_claude.core.tools.base import ToolResult

PENDING_RESULT = "still running; call agent_result again to keep waiting."


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
    report_path: str = ""
    truncated: bool = False

    @classmethod
    # 只复制终态与结果，不保留消息历史、Task 和整份 Context
    def from_run(
        cls, run: RunCheckpoint, session_id: str, parent_run_id: str, report_path: str = "",
    ) -> SubagentResult:
        unknown = any(
            tool.status in ("executing", "unknown")
            or bool(tool.result and tool.result.get("outcome_unknown"))
            for tool in run.tools
        )
        text, truncated = summarize(run.result)
        return cls(
            run.run_id, session_id, parent_run_id, run.status, run.reason, text,
            run.actual_tokens, run.estimated_tokens, run.elapsed_seconds, run.usage_complete,
            unknown, report_path, truncated,
        )

    # 前后台共用结果转换，任务正常返回也必须检查业务状态
    def as_tool_result(self) -> ToolResult:
        description = f"Subagent {self.status} (reason={self.reason or 'none'})."
        content = (
            f"[后端记录] run_id={self.run_id}\n{description}\n"
            f"用量：实际 {self.actual_tokens}，估算占用 {self.estimated_tokens}，"
            f"统计完整={self.usage_complete}\n"
            f"完整报告：{self.report_path or '不可用'}\n\n"
            f"[模型回答摘要，未经后端独立验证]\n{self.text or '(无文字回答)'}"
        )
        if self.truncated:
            content += "\n[回答已截断到 4000 字符，请按需读取完整报告。]"
        if self.status == "success" and not self.outcome_unknown:
            return ToolResult(content=content)
        return ToolResult(
            content=content,
            is_error=True, error_type="subagent_failed", outcome_unknown=self.outcome_unknown,
        )
