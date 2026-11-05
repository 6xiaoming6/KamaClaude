from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.bus.events import SubagentFinishedEvent
from kama_claude.core.config import SubagentConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.subagent.report import write_report
from kama_claude.core.subagent.result import SubagentResult

log = logging.getLogger(__name__)


class SubagentLimitError(ValueError):
    pass


@dataclass
class RootQuota:
    limits: SubagentConfig
    spawned: set[str] = field(default_factory=set)
    pending: set[str] = field(default_factory=set)


@dataclass
class ActiveSubagent:
    task: asyncio.Task[None]
    context: ExecutionContext
    session_id: str
    parent_run_id: str
    bus: EventBus
    root_run_id: str
    run_path: Path
    result: SubagentResult | None = None


# Core 共享任务与根 Run 配额；结果缓存淘汰不影响累计派生数。
class BackgroundTaskRegistry:
    # 完整 Context 只保留到任务结束，报告原文保存在磁盘。
    def __init__(self, max_results: int = 100) -> None:
        if max_results < 1:
            raise ValueError("max_results must be positive")
        self._tasks: dict[str, ActiveSubagent] = {}
        self._results: OrderedDict[str, SubagentResult] = OrderedDict()
        self._roots: dict[tuple[str, str], RootQuota] = {}
        self._max_results = max_results
        self._notifications: set[asyncio.Task[None]] = set()

    # Runner 重建只补充已提交的派生记录，不重置已有计数。
    def bind_root(
        self, root_run_id: str, session_id: str, limits: SubagentConfig,
        known_ids: Iterable[str] = (),
    ) -> None:
        quota = self._roots.setdefault((session_id, root_run_id), RootQuota(limits))
        quota.spawned.update(known_ids)

    # 单事件循环内同步检查并预留，不在检查与占位之间让出执行权。
    def reserve(self, run_id: str, root_run_id: str, session_id: str) -> None:
        for child_id, entry in list(self._tasks.items()):
            if entry.task.done():
                self._collect(child_id)
        quota = self._roots[(session_id, root_run_id)]
        active = sum(
            entry.session_id == session_id and entry.root_run_id == root_run_id
            and entry.result is None for entry in self._tasks.values()
        )
        if active + len(quota.pending) >= quota.limits.max_active:
            raise SubagentLimitError("Active subagent limit reached; wait for existing tasks.")
        if len(quota.spawned | quota.pending) >= quota.limits.max_total:
            raise SubagentLimitError("Total subagent limit reached for this root Run.")
        quota.pending.add(run_id)

    # 初始化失败或取消时释放预留；已登记任务的累计计数不会撤销。
    def release(self, run_id: str, root_run_id: str, session_id: str) -> None:
        quota = self._roots.get((session_id, root_run_id))
        if quota is not None:
            quota.pending.discard(run_id)

    # 登记成功才计入累计派生数，前后台和嵌套任务走同一入口。
    def register(
        self, run_id: str, task: asyncio.Task[None], context: ExecutionContext,
        *, session_id: str, parent_run_id: str, bus: EventBus,
        root_run_id: str, run_path: Path,
    ) -> ActiveSubagent:
        quota = self._roots[(session_id, root_run_id)]
        if run_id not in quota.pending:
            raise ValueError("subagent slot was not reserved")
        entry = ActiveSubagent(
            task, context, session_id, parent_run_id, bus, root_run_id, run_path,
        )
        self._tasks[run_id] = entry
        quota.pending.remove(run_id)
        quota.spawned.add(run_id)
        task.add_done_callback(lambda _: self._collect(run_id))
        return entry

    # 先原子保存报告再形成轻量结果；保存失败明确改变终态，保留未知副作用。
    def finalize(self, run_id: str) -> SubagentResult:
        entry = self._tasks[run_id]
        if entry.result is not None:
            return entry.result
        run = entry.context.run
        report_path = ""
        try:
            report_path = str(write_report(run, entry.run_path))
        except (OSError, UnicodeError, ValueError) as exc:
            log.exception("subagent report failed run_id=%s", run_id)
            if SubagentResult.from_run(run, entry.session_id, entry.parent_run_id).outcome_unknown:
                entry.context.mark_interrupted("report_save_failed")
            else:
                entry.context.mark_failed("report_save_failed")
            run.result += f"\nReport could not be saved: {exc}"
        entry.result = SubagentResult.from_run(
            run, entry.session_id, entry.parent_run_id, report_path,
        )
        self._results[run_id] = entry.result
        while len(self._results) > self._max_results:
            self._results.popitem(last=False)
        return entry.result

    # 查询先检查会话归属，已完成 Task 主动回收，缺失结果不重新派生。
    def get(self, run_id: str, session_id: str) -> ActiveSubagent | SubagentResult | None:
        entry = self._tasks.get(run_id)
        if entry is not None and entry.session_id != session_id:
            return None
        if entry is not None and entry.task.done():
            self._collect(run_id)
            entry = None
        result = (entry.result or entry) if entry is not None else self._results.get(run_id)
        return result if result is not None and result.session_id == session_id else None

    # shield 隔离查询超时和查询者取消；子任务自身取消仍返回它的已收尾结果。
    async def wait_result(
        self, run_id: str, session_id: str, wait_s: float,
    ) -> ActiveSubagent | SubagentResult | None:
        entry = self.get(run_id, session_id)
        if not isinstance(entry, ActiveSubagent) or wait_s == 0:
            return entry
        try:
            await asyncio.wait_for(asyncio.shield(entry.task), timeout=wait_s)
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception:
            # 任务异常由回收入口读取并转换为子任务结果。
            pass
        return self.get(run_id, session_id)

    # 统一通知终态；报告已经写入，通知失败不会触发任务重跑。
    async def notify_finished(self, run_id: str) -> None:
        entry = self._tasks[run_id]
        self.finalize(run_id)
        await entry.bus.publish_safe(self._finished_event(entry))

    # 事件只从已形成的后端结果构造，正常结束和启动前取消共用字段。
    def _finished_event(self, entry: ActiveSubagent) -> SubagentFinishedEvent:
        result = entry.result
        assert result is not None
        return SubagentFinishedEvent(
            run_id=result.run_id, parent_run_id=entry.parent_run_id, status=result.status,
            reason=result.reason, actual_tokens=result.actual_tokens,
            estimated_tokens=result.estimated_tokens, elapsed_seconds=result.elapsed_seconds,
            usage_complete=result.usage_complete, ts=datetime.now(UTC).isoformat(),
        )

    # 回收异常与活跃名额；协程尚未开始便取消时，也保存报告并补发一次通知。
    def _collect(self, run_id: str) -> None:
        entry = self._tasks.get(run_id)
        if entry is None:
            return
        missing_finalization = entry.result is None
        if missing_finalization:
            run = entry.context.run
            if entry.task.cancelled():
                entry.context.mark_failed("cancelled")
            else:
                error = entry.task.exception()
                if error is not None:
                    log.error("subagent task failed run_id=%s error=%s", run_id, error)
                    if run.status == "running":
                        entry.context.mark_failed("subagent_error")
                        run.result = str(error)
            self.finalize(run_id)
            notice = asyncio.create_task(entry.bus.publish_safe(self._finished_event(entry)))
            self._notifications.add(notice)
            notice.add_done_callback(self._notifications.discard)
        elif not entry.task.cancelled():
            entry.task.exception()
        self._tasks.pop(run_id)

    # 会话关闭清除对应根配额；Core 退出取消全部，等待工具完成有界清理。
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
        for key in list(self._roots):
            if session_id is None or key[0] == session_id:
                del self._roots[key]
