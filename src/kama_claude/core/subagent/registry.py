from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from kama_claude.core.bus.events import SubagentFinishedEvent
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.subagent.result import SubagentResult

log = logging.getLogger(__name__)


@dataclass
class ActiveSubagent:
    task: asyncio.Task[None]
    context: ExecutionContext
    session_id: str
    parent_run_id: str
    bus: EventBus


# 管理 Core 中子任务的生命周期：会话隔离、终态缓存、批量取消
class BackgroundTaskRegistry:
    # Core 共享活跃任务，完成后最多保留指定数量的轻量结果
    def __init__(self, max_results: int = 100) -> None:
        if max_results < 1:
            raise ValueError("max_results must be positive")
        self._tasks: dict[str, ActiveSubagent] = {}
        self._results: OrderedDict[str, SubagentResult] = OrderedDict()
        self._max_results = max_results
        self._notifications: set[asyncio.Task[None]] = set()

    # 注册一个后台任务及其执行上下文
    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
        *, session_id: str, parent_run_id: str, bus: EventBus,
    ) -> None:
        self._tasks[run_id] = ActiveSubagent(task, context, session_id, parent_run_id, bus)
        task.add_done_callback(lambda _: self._collect(run_id))

    # 查询只限所属会话；已完成任务释放 Context，仅返回轻量结果
    def get(self, run_id: str, session_id: str) -> ActiveSubagent | SubagentResult | None:
        entry = self._tasks.get(run_id)
        if entry is not None and entry.task.done():
            self._collect(run_id)
            entry = None
        result = entry or self._results.get(run_id)
        return result if result is not None and result.session_id == session_id else None

    # 主动取走异常，移除已完成 Task，并裁剪结果缓存
    def _collect(self, run_id: str) -> None:
        entry = self._tasks.pop(run_id, None)
        if entry is None:
            return
        run = entry.context.run
        never_started = entry.task.cancelled() and run.status == "running"
        if never_started:
            entry.context.mark_failed("cancelled")
        elif not entry.task.cancelled():
            error = entry.task.exception()
            if error is not None:
                log.error("subagent task failed run_id=%s error=%s", run_id, error)
                if run.status == "running":
                    entry.context.mark_failed("subagent_error")
                    run.result = str(error)
        self._results[run_id] = SubagentResult.from_run(run, entry.session_id, entry.parent_run_id)
        while len(self._results) > self._max_results:
            self._results.popitem(last=False)
        # 协程还没获得执行机会就被取消时，无法进入其 finally，补发一次结束事件
        if never_started:
            notice = asyncio.create_task(entry.bus.publish_safe(SubagentFinishedEvent(
                run_id=run_id, parent_run_id=entry.parent_run_id, status=run.status,
                reason=run.reason, ts=datetime.now(UTC).isoformat(),
            )))
            self._notifications.add(notice)
            notice.add_done_callback(self._notifications.discard)

    # 会话关闭只取消本会话，Core 关闭取消全部，等待工具的取消清理结束
    async def cancel(self, *, session_id: str | None = None) -> None:
        tasks = [
            entry.task for entry in list(self._tasks.values())
            if session_id is None or entry.session_id == session_id
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._notifications:
            await asyncio.gather(*list(self._notifications), return_exceptions=True)
