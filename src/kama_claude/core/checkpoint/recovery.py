from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kama_claude.core.checkpoint.checkpoint import Checkpoint, RunCheckpoint

if TYPE_CHECKING:
    from kama_claude.core.session.store import SessionStore

RECOVERY_REQUIRED = -32030
RECOVERY_CONFLICT = -32031


# 返回恢复决定的记录时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionRecovery:
    # 只依赖存储，不持有会话锁、业务 Task 或另一份运行状态
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    # 读取检查点并识别遗留运行，必要时保存修正后的状态
    def recover_orphaned_run(self, sid: str, *, running: bool) -> Checkpoint:
        cp = self._store.ensure_checkpoint(sid)
        run = cp.run
        # 异常中断，加载时更新为中断
        if run and run.status == "running" and not running:
            run.status = "interrupted"
            run.reason = "core_restarted"
            for tool in run.tools:
                if tool.status == "executing":
                    tool.status = "unknown"
            cp.session["status"] = "interrupted"
            self._store.checkpoints.save(cp)
        elif (
            run and run.status == "interrupted"
            and cp.session["status"] == "active" and not running
        ):
            # Loop 已提交中断，补齐未完成的会话收尾，不改变恢复位置
            cp.session["status"] = "interrupted"
            self._store.checkpoints.save(cp)
        elif (
            run and run.status in ("success", "failed")
            and cp.session["status"] == "active" and not running
        ):
            cp.session["status"] = (
                "closed" if cp.session["mode"] == "one_shot" else "waiting_for_input"
            )
            self._store.checkpoints.save(cp)
        return cp

    # 检查这次恢复是否合法，然后把检查点改成用户选择的状态
    def prepare_resume(
        self,
        cp: Checkpoint,
        expected_revision: int,
        action: Literal["continue", "retry_tool", "abort"],
    ) -> RunCheckpoint:
        # 执行时再引入协议异常，避免会话包初始化时产生循环依赖
        from kama_claude.core.bus.envelope import HandlerError

        run = cp.run
        # 用户可能仍停留在旧页面，先校验版本，不能覆盖较新的决定
        if cp.revision != expected_revision:
            raise HandlerError(RECOVERY_CONFLICT, "checkpoint changed; reopen session")
        if cp.session["status"] == "closed" or not run or run.status != "interrupted":
            raise HandlerError(RECOVERY_REQUIRED, "no interrupted run to resume")
        if action == "abort":
            self._abort(cp, run)
        else:
            # 检查是否有结果未知的工具调用
            unknown = [t for t in run.tools if t.status in ("executing", "unknown")]

            # 一些必要性前置检查
            from kama_claude.core.subagent.store import SubagentResultStore, SubagentStoreError

            # 仅复用已落盘且结果明确的成果；没有完成记录的子任务树仍禁止恢复。
            results = SubagentResultStore(self._store.runs_dir(str(cp.session["id"])))
            unfinished = False
            for child_id, state in run.subagents.items():
                if state not in {"running", "unknown", "background"}:
                    continue
                try:
                    saved = results.load(child_id, str(cp.session["id"]))
                except SubagentStoreError as exc:
                    raise HandlerError(RECOVERY_REQUIRED, str(exc)) from exc
                if saved is None or saved.outcome_unknown:
                    unfinished = True
                    break
            if unfinished or any(tool.name == "spawn_agent" for tool in unknown):
                raise HandlerError(
                    RECOVERY_REQUIRED, "unfinished subagent recovery is not supported"
                )
            if (
                (run.max_run_seconds and run.elapsed_seconds >= run.max_run_seconds)
                or (run.max_total_tokens
                    and run.actual_tokens + run.estimated_tokens >= run.max_total_tokens)
            ):
                raise HandlerError(RECOVERY_REQUIRED, "run budget exhausted; abort this run")
            if Path(run.workspace).resolve() != Path.cwd().resolve():
                raise HandlerError(RECOVERY_REQUIRED, f"start Core in workspace: {run.workspace}")


            if unknown and action != "retry_tool":
                raise HandlerError(RECOVERY_REQUIRED, "tool outcome unknown; verify before retry")
            if action == "retry_tool" and not unknown:
                raise HandlerError(RECOVERY_REQUIRED, "there is no unknown tool to retry")
            # 明确确认才允许重试，允许重试后更新状态
            for tool in unknown:
                tool.status = "pending"
            run.status = "running"
            run.reason = None
            cp.session["status"] = "active"
        ts = _now()
        run.decisions.append({"action": action, "ts": ts})
        cp.session["updated_at"] = ts
        return run

    # 终止原 Run，为未完成调用补充中断说明，保证后续消息可以合法续聊
    def _abort(self, cp: Checkpoint, run: RunCheckpoint) -> None:
        run.status = "failed"
        run.phase = "finished"
        run.reason = "recovery_aborted"
        for tool in run.tools:
            if tool.status == "completed":
                continue
            block = {
                "type": "tool_result",
                "tool_use_id": tool.id,
                "is_error": True,
                "content": (
                    "Run aborted by user. Operation outcome is UNKNOWN; "
                    "do not assume failure or automatically repeat."
                    if tool.status in ("unknown", "executing")
                    else "Run aborted by user before this tool produced a result."
                ),
            }
            self._store.record_message(cp, "user", [block], run.run_id)
            last = cp.messages[-1] if cp.messages else None
            if (
                last and last["role"] == "user"
                and isinstance(last["content"], list)
                and all(b.get("type") == "tool_result" for b in last["content"])
            ):
                last["content"].append(block)
            else:
                cp.messages.append({"role": "user", "content": [block]})
        cp.session["status"] = (
            "closed" if cp.session["mode"] == "one_shot" else "waiting_for_input"
        )
