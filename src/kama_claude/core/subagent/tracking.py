from __future__ import annotations

from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint, SubagentNotification
from kama_claude.core.tools.base import ToolResult

NOTICE_MARKER = "[KamaClaude subagent completion notice]"


class SubagentTracker:
    # 跟踪数据仍写在原 Run；本对象不维护第二份任务状态。
    def __init__(self, run: RunCheckpoint) -> None:
        self.run = run

    async def handle_event(self, event: Any) -> None:
        run = self.run
        if getattr(event, "type", "") == "subagent.started":
            if event.parent_run_id == run.run_id or event.parent_run_id in run.subagents:
                current = run.tools[run.tool_index] if run.tool_index < len(run.tools) else None
                background = (
                    current is not None and current.name == "spawn_agent"
                    and bool(current.input.get("run_in_background"))
                ) or run.subagents.get(event.parent_run_id) == "background"
                run.subagents[event.run_id] = "background" if background else "running"
        elif getattr(event, "type", "") == "subagent.finished":
            if event.run_id in run.subagents:
                if run.subagents[event.run_id] == "collected":
                    return  # 已交接的前台结果和迟到事件不再提醒。
                if event.status == "interrupted":
                    run.subagents[event.run_id] = "unknown"
                elif run.subagents[event.run_id] != "background":
                    run.subagents[event.run_id] = event.status
                if event.parent_run_id == run.run_id:
                    run.subagent_notifications.setdefault(
                        event.run_id, SubagentNotification(status=event.status),
                    )

    # 在请求准备边界合并队列；只追加标准上下文，不把通知当成结果交接。
    def deliver_notifications(self, messages: list[dict[str, Any]]) -> bool:
        pending = [
            (child_id, notice) for child_id, notice in self.run.subagent_notifications.items()
            if not notice.delivered and self.run.subagents.get(child_id) != "collected"
        ]
        if not pending:
            return False
        lines = [
            f"Subagent `{child_id}` has finished with status `{notice.status}`. "
            f"Call agent_result(run_ids=['{child_id}']) to collect its result."
            for child_id, notice in pending
        ]
        messages.append({"role": "user", "content": NOTICE_MARKER + "\n" + "\n".join(lines)})
        for _, notice in pending:
            notice.delivered = True
        return True

    # 结果正式提交前登记已收集；通用 Loop 不需要认识 agent_result 的业务约定。
    def record_result(self, result: ToolResult) -> None:
        for child_id in result.collected_subagents:
            if child_id in self.run.subagents:
                self.run.subagents[child_id] = "collected"
                self.run.subagent_notifications.pop(child_id, None)

    # 完成事件只表示子任务结束，后台结果正式交接后才允许父任务提交。
    def finish_error(self) -> ToolResult | None:
        pending = [
            run_id for run_id, state in self.run.subagents.items()
            if state in {"running", "background", "unknown"}
        ]
        if not pending:
            return None
        return ToolResult(
            "Collect outstanding subagent results with agent_result before finish. "
            f"Run IDs: {', '.join(pending)}. To stop unneeded tasks, use agent_cancel, "
            "then collect their terminal results. "
            "Unknown outcomes still require user verification.",
            is_error=True, error_type="invalid_operation",
        )
