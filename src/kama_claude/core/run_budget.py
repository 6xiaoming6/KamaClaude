from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kama_claude.core.bus.events import RunBudgetEvent
from kama_claude.core.events.bus import EventBus

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
    from kama_claude.core.checkpoint.persistence import RunPersistence
    from kama_claude.core.tools.base import ToolResult


class BudgetExceeded(RuntimeError):
    pass


# 预算事件不依赖具体 Provider；Loop 终止和模型请求共用此入口。
async def publish_budget(
    bus: EventBus, budget: RunBudget, method: str = "", estimated_input: int = 0,
    reason: str = "", *, trigger_tokens: int = 0,
) -> None:
    run = budget.run
    await bus.publish_safe(RunBudgetEvent(
        run_id=run.run_id, method=method, estimated_input_tokens=estimated_input,
        actual_tokens=run.actual_tokens, estimated_tokens=run.estimated_tokens,
        elapsed_seconds=run.elapsed_seconds, max_total_tokens=run.max_total_tokens,
        max_run_seconds=run.max_run_seconds, usage_complete=run.usage_complete,
        reason=reason, trigger_tokens=trigger_tokens,
        child_actual_tokens=run.child_actual_tokens,
        child_estimated_tokens=run.child_estimated_tokens,
        ts=datetime.now(UTC).isoformat(),
    ))


class RunBudget:
    # 累计值只放在 Run，单调时钟基点只在本次进程内有效
    def __init__(
        self, run: RunCheckpoint, *, parent: RunBudget | None = None,
        persistence: RunPersistence | None = None,
    ) -> None:
        self.run = run
        self.parent = parent
        self._persistence = persistence
        self._save_error: Exception | None = None
        self._last_tick = time.monotonic()

    # 在检查点边界累计执行时间，恢复不计入离线等待
    def tick(self) -> None:
        now = time.monotonic()
        self.run.elapsed_seconds += max(0.0, now - self._last_tick)
        self._last_tick = now

    # 返回本次运行剩余时间，None 表示没有开启时间限制
    def remaining_seconds(self) -> float | None:
        self.tick()
        own = (
            max(0.0, self.run.max_run_seconds - self.run.elapsed_seconds)
            if self.run.max_run_seconds else None
        )
        parent = self.parent.remaining_seconds() if self.parent is not None else None
        values = [value for value in (own, parent) if value is not None]
        return min(values) if values else None

    # 检查已知消耗及本次请求预留，未知请求不能作为零消耗
    def check(self, reservation: int = 0) -> None:
        if self._save_error is not None:
            raise self._save_error
        if self.run.progress.stopped:
            raise BudgetExceeded("no_progress")
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise BudgetExceeded("run_time_budget_exceeded")
        total = self.run.actual_tokens + self.run.estimated_tokens
        if self.run.max_total_tokens and (
            total + reservation > self.run.max_total_tokens
            or (reservation == 0 and total >= self.run.max_total_tokens)
        ):
            raise BudgetExceeded("run_token_budget_exceeded")
        if self.parent is not None:
            self.parent.check(reservation)
            if self.parent.run.status != "running":
                raise BudgetExceeded("parent_run_stopped")

    # 请求前占用输入估算与输出上限，随后必须先保存再调用模型
    def reserve(self, tokens: int) -> None:
        self.check(tokens)
        # 检查与全链占用之间没有 await；并发兄弟任务不能透支同一余额。
        self._adjust(0, tokens)

    # 收到有效 Usage 后用实际消耗替换本次预留，旧未知占用保持不变
    def settle(self, reservation: int, actual: int) -> None:
        self._adjust(actual, -reservation)

    def _adjust(self, actual: int, estimated: int, *, child: bool = False) -> None:
        self.run.actual_tokens += actual
        self.run.estimated_tokens += estimated
        if child:
            self.run.child_actual_tokens += actual
            self.run.child_estimated_tokens += estimated
        self.run.usage_complete = self.run.estimated_tokens == 0
        if self.parent is not None:
            self.parent._adjust(actual, estimated, child=True)

    def block(self, error: Exception) -> None:
        self._save_error = error
        if self.parent is not None:
            self.parent.block(error)

    # 子任务没有会话检查点，但必须先提交祖先的共享占用，再允许真正请求模型。
    def save_ancestors(self) -> None:
        parent = self.parent
        while parent is not None:
            parent.tick()
            if parent._persistence is not None:
                try:
                    parent._persistence.save()
                except Exception as exc:
                    self.block(exc)
                    raise
            parent = parent.parent

    # 只统计完整调用的最终错误，权限拒绝和未知结果仍走自己的流程
    def record_tool_result(self, name: str, params: dict[str, object], result: ToolResult) -> bool:
        run = self.run
        if (
            not result.is_error or result.outcome_unknown
            or result.error_type == "permission_denied"
        ):
            run.failure_fingerprint = ""
            run.repeated_failures = 0
            return False
        content = json.dumps(
            [name, params, result.error_type, result.content], sort_keys=True, ensure_ascii=False,
        )
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        run.repeated_failures = (
            run.repeated_failures + 1 if fingerprint == run.failure_fingerprint else 1
        )
        run.failure_fingerprint = fingerprint
        if run.repeat_failure_limit and run.repeated_failures >= 3:
            result.content += (
                "\n[Repeated failure: change the approach or arguments before retrying.]"
            )
        return bool(run.repeat_failure_limit and run.repeated_failures >= run.repeat_failure_limit)
