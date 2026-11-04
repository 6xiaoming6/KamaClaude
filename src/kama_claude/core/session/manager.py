from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.bus.events import (
    RunFinishedEvent,
    RunInterruptedEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from kama_claude.core.checkpoint.checkpoint import Checkpoint, CheckpointError
from kama_claude.core.checkpoint.recovery import RECOVERY_REQUIRED, SessionRecovery
from kama_claude.core.events.bus import EventBus
from kama_claude.core.runs import new_run_id
from kama_claude.core.session.model import Session, SessionMode
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
log = logging.getLogger(__name__)


# 返回当前 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 加载磁盘会话索引，并由服务端持有独立于客户端连接的运行任务
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        provider_factory: Callable[[], LLMProvider] | None = None,
        permission_manager: PermissionManager | None = None,
    ) -> None:
        self._store = store
        self._recovery = SessionRecovery(store)
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._provider_factory = provider_factory
        self._permissions = permission_manager
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._load_errors: dict[str, str] = {}
        self._skill_loader = SkillLoader()
        for sid in store.session_ids():
            try:
                session = store.read_meta(sid)
                self._sessions[sid] = session
                self._locks[sid] = asyncio.Lock()
                if store.checkpoints.exists(sid):
                    cp = self._recovery.recover_orphaned_run(sid, running=False)
                    self._sessions[sid] = Session.from_dict(cp.session)
            except (CheckpointError, OSError, ValueError, KeyError) as exc:
                self._load_errors[sid] = str(exc)

    # 创建新会话并提交初始检查点
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(sid, mode, "active", title, ts, ts)
        self._store.write_meta(session)
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        await self._bus.publish_safe(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 接受新消息并通过 shield 等待，客户端断开不会取消已经登记的业务任务
    async def send_message(
        self,
        sid: str,
        content: str,
        *,
        run_id: str | None = None,
        wait: bool = True,
    ) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked() or self.is_running(sid):
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")
            # 读取最近检查点或者新建检查点，然后恢复检查点并修改状态
            cp = self._recovery.recover_orphaned_run(sid, running=self.is_running(sid))
            self._sessions[sid] = Session.from_dict(cp.session)
            session = self._get_session(sid)
            if cp.run and cp.run.status == "interrupted":
                raise HandlerError(RECOVERY_REQUIRED, "resume or abort the interrupted run first")
            was_waiting = session.status == "waiting_for_input"
            goal = content
            system_prompt: str | None = None
            tools: list[str] | None = None
            skill = None
            arguments = ""

            # 如果检测到'/'开头，就说明指定了skill，就加载skill的内容
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                if not parts:
                    raise HandlerError(-32602, "skill name is empty")
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(parts[0])
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt = goal
                    tools = skill.allowed_tools or None
            cp.messages.append({"role": "user", "content": content})

            # session准备好了，状态设为active
            session.status = "active"
            session.title = session.title or content[:40]
            session.updated_at = _now()
            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)

            # 先保存目标和初始 Run，再登记后台执行，避免调度窗口丢任务
            runner = self._runner_factory()
            # 保存初始检查点，记录初始信息
            runner.prepare_checkpoint(
                cp, session, goal, run_id, system_prompt, tools, self._store,
            )
            # 先提交再启动；Core 持有任务，前端断线后仍可继续
            task = self._start(
                sid, run_id,
                self._run_session(
                    sid, goal, run_id, runner=runner,
                    system_prompt=system_prompt, tools=tools,
                ),
            )
        if was_waiting:
            await self._bus.publish_safe(SessionResumedEvent(session_id=sid, ts=_now()))
        await self._bus.publish_safe(
            SessionMessageReceivedEvent(
                session_id=sid,
                content=content,
                ts=_now(),
            )
        )
        if skill is not None:
            await self._bus.publish_safe(
                SkillInvokedEvent(
                    skill_name=skill.name,
                    arguments=arguments,
                    run_id=run_id,
                    ts=_now(),
                )
            )
        if not wait:
            return run_id
        return await asyncio.shield(task)

    # 真正的执行入口，在会话锁内执行 Runner，再依据已提交状态通知前端
    async def _run_session(
        self,
        sid: str,
        goal: str,
        run_id: str,
        *,
        checkpoint: Checkpoint | None = None,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        runner: AgentRunner | None = None,
    ) -> str:
        # 获得该会话的锁
        async with self._locks[sid]:
            session = self._get_session(sid)
            # 每次运行创建新的runner，runner内部把需要的组件组装好运行agent loop并保存检查点
            runner = runner or self._runner_factory()
            await runner.run_and_capture(
                goal,
                run_id=run_id,
                session=session,
                store=self._store,
                system_prompt_override=system_prompt,
                tool_whitelist=tools,
                checkpoint=checkpoint,
            )
            # Runner 已提交 Run 和会话终态
            cp = self._store.checkpoints.load(sid)
            session = Session.from_dict(cp.session)
            self._sessions[sid] = session
            if session.status == "interrupted":
                return run_id
            if session.mode == "one_shot":
                await self._bus.publish_safe(
                    SessionClosedEvent(session_id=sid, ts=session.updated_at)
                )
            else:
                await self._bus.publish_safe(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            return run_id

    # 创建并登记业务任务，客户端只等待结果而不拥有任务
    def _start(
        self, sid: str, run_id: str, execution: Coroutine[Any, Any, str],
    ) -> asyncio.Task[str]:
        task = asyncio.create_task(execution)
        # Core 持有业务 Task，客户端连接只负责等待或观察
        self._tasks[sid] = task

        # 回收同一个任务的登记，避免迟到回调移除后续运行
        def done(completed: asyncio.Task[str]) -> None:
            if self._tasks.get(sid) is completed:
                self._tasks.pop(sid, None)
            if not completed.cancelled() and completed.exception() is not None:
                error = completed.exception()
                log.error("session run failed sid=%s error=%s", sid, error)
                if isinstance(error, Exception):
                    asyncio.create_task(self._notify_failed_task(sid, run_id))

        task.add_done_callback(done)
        return task

    # 判断会话是否仍有正在执行的服务端任务
    def is_running(self, sid: str) -> bool:
        task = self._tasks.get(sid)
        return task is not None and not task.done()

    # 列出会话摘要，损坏快照仍显示为不可恢复而非偷偷读取旧历史
    def list_sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sid in list(self._sessions):
            try:
                # 读取每个会话最新的检查点快照并返回
                cp = self._recovery.recover_orphaned_run(sid, running=self.is_running(sid))
                self._sessions[sid] = Session.from_dict(cp.session)
                rows.append(
                    {
                        **cp.session,
                        "revision": cp.revision,
                        "run": cp.run.model_dump(
                            mode="json", include={"run_id", "status", "phase", "step", "reason"}
                        )
                        if cp.run
                        else None,
                    }
                )
            except (CheckpointError, OSError, ValueError) as exc:
                self._load_errors[sid] = str(exc)
        for sid, error in self._load_errors.items():
            rows = [row for row in rows if row["id"] != sid]
            rows.append({"id": sid, "title": sid, "status": "unavailable", "error": error})
        return sorted(rows, key=lambda row: row.get("updated_at", ""), reverse=True)

    # 返回已提交历史、执行位置以及当前仍有效的审批请求
    async def open(self, sid: str) -> dict[str, Any]:
        self._get_session(sid)
        cp = self._recovery.recover_orphaned_run(sid, running=self.is_running(sid))
        self._sessions[sid] = Session.from_dict(cp.session)
        pending = self._permissions.pending_for_session(sid) if self._permissions else []
        for event in pending:
            event["run_id"] = cp.run.run_id if cp.run else ""
        return {
            "session": cp.session,
            "revision": cp.revision,
            "messages": cp.messages,
            "run": cp.run.model_dump(mode="json") if cp.run else None,
            "running": self.is_running(sid),
            "pending_permissions": pending,
        }

    # 在会话锁内应用恢复决定，保存成功后才启动原 Run
    async def resume(
        self,
        sid: str,
        expected_revision: int,
        action: Literal["continue", "retry_tool", "abort"] = "continue",
    ) -> dict[str, Any]:
        self._get_session(sid)
        if self.is_running(sid) or self._locks[sid].locked():
            raise HandlerError(SESSION_BUSY, "run is already running")
        async with self._locks[sid]:
            cp = self._store.checkpoints.load(sid)
            # 检查这次恢复是否合法，然后把检查点改成用户选择的状态
            run = self._recovery.prepare_resume(cp, expected_revision, action)
            self._store.checkpoints.save(cp)
            self._sessions[sid] = Session.from_dict(cp.session)
            if action == "abort":
                await self._bus.publish_safe(
                    RunFinishedEvent(
                        run_id=run.run_id, status="failed", reason=run.reason,
                        steps=run.step, ts=_now(),
                    )
                )
            else:
                self._start(
                    sid, run.run_id,
                    self._run_session(sid, run.goal, run.run_id, checkpoint=cp),
                )
            return {"run_id": run.run_id, "status": run.status, "revision": cp.revision}

    # 显式关闭会话，进行中的运行必须先结束
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        if self.is_running(sid) or self._locks[sid].locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with self._locks[sid]:
            cp = self._store.ensure_checkpoint(sid)
            if cp.run and cp.run.status == "interrupted":
                cp.run.status = "failed"
                cp.run.phase = "finished"
                cp.run.reason = "session_closed"
            session.status = "closed"
            session.updated_at = _now()
            cp.session = session.to_dict()
            self._store.checkpoints.save(cp)
            await self._bus.publish_safe(SessionClosedEvent(session_id=sid, ts=_now()))

    # 空闲会话的手动压缩与正常执行复用同一权威工作历史
    async def compact(self, sid: str, focus: str = "") -> Any:
        session = self._get_session(sid)
        if self.is_running(sid) or self._locks[sid].locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if session.status in ("closed", "interrupted"):
            raise HandlerError(RECOVERY_REQUIRED, "open an idle session before compaction")
        if self._provider is None and self._provider_factory is not None:
            self._provider = self._provider_factory()
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with self._locks[sid]:
            from kama_claude.core.bus.commands import SessionCompactResult
            from kama_claude.core.compact.compactor import Compactor

            result = await Compactor(
                self._bus,
                self._store.session_dir(sid),
                sid,
            ).compact_messages(self._store.read_messages(sid), self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed")
            self._store.write_compacted(
                sid,
                [
                    {"role": "user", "content": result.summary_text},
                    {
                        "role": "assistant",
                        "content": "Understood, I'll continue from this summary.",
                    },
                ],
            )
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.summary_tokens),
            )

    # 读取权威工作历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # Core 关闭时等待所有受管任务完成检查点收尾
    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 后台任务异常结束后重新同步权威状态，避免客户端永久等待未到达的终态
    async def _notify_failed_task(self, sid: str, run_id: str) -> None:
        if self.is_running(sid):
            return
        revision = 0
        try:
            cp = self._store.checkpoints.load(sid)
            if not cp.run or cp.run.run_id != run_id:
                return
            cp = self._recovery.recover_orphaned_run(sid, running=False)
            self._sessions[sid] = Session.from_dict(cp.session)
            revision = cp.revision
            if cp.run is not None and cp.run.status in ("success", "failed"):
                await self._bus.publish_safe(
                    RunFinishedEvent(
                        run_id=run_id,
                        status=cp.run.status,
                        reason=cp.run.reason,
                        steps=cp.run.step,
                        ts=_now(),
                    )
                )
                return
        except CheckpointError:
            log.exception("cannot reconcile failed run sid=%s", sid)
        await self._bus.publish_safe(
            RunInterruptedEvent(
                run_id=run_id,
                session_id=sid,
                reason="checkpoint_or_run_failure",
                revision=revision,
                ts=_now(),
            )
        )

    # 返回磁盘索引对应会话，损坏快照以明确错误拒绝
    def _get_session(self, sid: str) -> Session:
        if sid in self._load_errors:
            raise HandlerError(RECOVERY_REQUIRED, self._load_errors[sid])
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
