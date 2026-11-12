from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, ValidationError

import kama_claude
from kama_claude.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    CoreShutdownCommand,
    CoreShutdownResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionInterruptCommand,
    SessionInterruptResult,
    SessionListCommand,
    SessionListResult,
    SessionOpenCommand,
    SessionOpenResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSetPermissionModeCommand,
    SessionSetPermissionModeResult,
)
from kama_claude.core.bus.envelope import EventPushEnvelope, HandlerError
from kama_claude.core.config import KamaConfig, get_config
from kama_claude.core.events.bus import EventBus
from kama_claude.core.execution.owner import SandboxOwner, cleanup_orphans
from kama_claude.core.llm.provider import DeepseekProvider
from kama_claude.core.logging_setup import config_summary, setup_logging
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.storage import load_policy_file
from kama_claude.core.runner import AgentRunner
from kama_claude.core.runs import events_file, new_run_id
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.framing import encode_frame
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.sender import ConnectionSender
from kama_claude.core.transport.socket_server import (
    SocketServer,
    get_connection_sender,
    get_connection_writer,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: KamaConfig | None = None
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._task_registry = BackgroundTaskRegistry()
        self._shutdown = asyncio.Event()

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=kama_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
            pid=os.getpid(),
        )

    # 只有记录中的同一个进程才能被停止，避免端口或 PID 复用误停其他 Core。
    async def _shutdown_handler(self, params: dict[str, Any]) -> CoreShutdownResult:
        cmd = CoreShutdownCommand.model_validate(params)
        if (cmd.expected_pid != os.getpid()
                or abs(cmd.expected_created_at - psutil.Process().create_time()) > 0.001):
            raise HandlerError(-32602, "Core process identity changed; refusing shutdown")
        asyncio.get_running_loop().call_soon(self._shutdown.set)
        return CoreShutdownResult()

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    # 这里用trace记录core内部的事件传递
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        run_id = new_run_id()
        if self._broadcaster:
            self._broadcaster.bind_run(run_id, session.id)
        await self._sessions.send_message(session.id, cmd.goal, run_id=run_id, wait=False)
        return AgentRunResult(run_id=run_id)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(mode=cmd.mode, title=cmd.title)
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = new_run_id()
        if self._broadcaster:
            self._broadcaster.bind_run(run_id, cmd.session_id)
        await self._sessions.send_message(cmd.session_id, cmd.content, run_id=run_id)
        return SessionSendMessageResult(run_id=run_id)

    # 列出可打开的历史会话与中断状态
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        SessionListCommand.model_validate(params)
        return SessionListResult(sessions=self._sessions.list_sessions())

    # 原子地绑定会话事件并返回已提交快照，不在两者之间等待网络
    async def _session_open_handler(self, params: dict[str, Any]) -> SessionOpenResult:
        assert self._sessions is not None and self._broadcaster is not None
        cmd = SessionOpenCommand.model_validate(params)
        result = await self._sessions.open(cmd.session_id)
        writer = get_connection_writer()
        self._broadcaster.unsubscribe(writer)
        if result["run"]:
            self._broadcaster.bind_run(result["run"]["run_id"], cmd.session_id)
        self._broadcaster.subscribe(get_connection_sender(), ["*"], f"session:{cmd.session_id}")
        result["event_seq"] = self._broadcaster.sequence(cmd.session_id)
        return SessionOpenResult(**result)

    # 校验恢复决定并立即返回已登记的运行身份
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        state = await self._sessions.open(cmd.session_id)
        if self._broadcaster and state["run"]:
            self._broadcaster.bind_run(state["run"]["run_id"], cmd.session_id)
        result = await self._sessions.resume(
            cmd.session_id, cmd.expected_revision, cmd.action,
        )
        return SessionResumeResult(**result)

    # 分页返回完整原文；固定快照末尾避免加载期间重复纳入实时追加。
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages, cursor, end = self._sessions.get_history_page(
            cmd.session_id, cmd.cursor, cmd.history_end, include_metadata=cmd.include_metadata,
        )
        return SessionGetHistoryResult(messages=messages, next_cursor=cursor, history_end=end)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received request_id=%s decision=%s",
            cmd.request_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            raise HandlerError(-32603, "permission manager unavailable")
        # 调用permission_manager.respond方法，将对应工具调用的feture设置为done
        try:
            self._permission_manager.respond(
                cmd.request_id, cmd.decision, session_id=cmd.session_id, run_id=cmd.run_id,
            )
        except ValueError as exc:
            raise HandlerError(-32602, str(exc)) from exc
        return PermissionRespondResult()

    # 手动压缩模型工作上下文并提交检查点，完整 thread 保持不变
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result

    # 控制请求独立于普通请求额度，审批或长 RPC 占满时仍能取消业务 Run。
    async def _session_interrupt_handler(self, params: dict[str, Any]) -> SessionInterruptResult:
        assert self._sessions is not None
        cmd = SessionInterruptCommand.model_validate(params)
        result = await self._sessions.interrupt(cmd.session_id, cmd.run_id, wait=cmd.wait)
        return SessionInterruptResult(**result)

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 用户切换会话审批模式；模型工具参数不能修改这一设置。
    async def _session_permission_mode_handler(
        self, params: dict[str, Any],
    ) -> SessionSetPermissionModeResult:
        assert self._sessions is not None
        try:
            cmd = SessionSetPermissionModeCommand.model_validate(params)
        except ValidationError as exc:
            raise HandlerError(-32602, "Invalid permission mode parameters", str(exc)) from exc
        mode = await self._sessions.set_permission_mode(cmd.session_id, cmd.mode)
        return SessionSetPermissionModeResult(permission_mode=mode)

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        sender = get_connection_sender()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, sender, cmd.topics
            )
        assert self._broadcaster is not None
        # 客户端发送订阅请求时，广播器会保存当前客户端的订阅内容，以后事件发布时广播给订阅了的客户端
        sub_id = self._broadcaster.subscribe(sender, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        sender: ConnectionSender,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            for candidate in Path(
                os.environ.get("KAMA_SESSIONS_DIR", "~/.kama/sessions")
            ).expanduser().glob(
                f"*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            # 回放可以等待写出，避免快速灌满健康连接的队列。
            await sender.send(encode_frame(envelope))
            count += 1
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.kama/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  legacy deny entries=%d",
            self._config.permission.timeout_s,
            sum(value == "deny" for value in load_policy_file(policy_file).values()),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        # Trace 初始化后注册广播器，之后发布的事件都会经过广播器。
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path(os.environ.get("KAMA_SESSIONS_DIR", "~/.kama/sessions")).expanduser()
        store = SessionStore(sessions_root)
        assert self._config is not None
        config = self._config
        sandbox_owner = SandboxOwner.for_core(sessions_root, config.host, config.port)
        self._mcp_manager = McpServerManager(config.tool_search)
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                config,
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                task_registry=self._task_registry,
                sandbox_owner=sandbox_owner,
            ),
            bus=self._bus,
            provider_factory=lambda: DeepseekProvider(config.llm.default_model),
            compaction=config.compaction,
            memory=config.memory,
            task_registry=self._task_registry,
            permission_manager=self._permission_manager,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
        )
        server.register("core.ping", self._ping_handler, control=True)
        server.register("core.shutdown", self._shutdown_handler, control=True)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.open", self._session_open_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("session.interrupt", self._session_interrupt_handler, control=True)
        server.register("permission.respond", self._permission_respond_handler, control=True)
        server.register("session.compact", self._session_compact_handler)
        server.register(
            "session.set_permission_mode", self._session_permission_mode_handler, control=True,
        )

        if config.execution.backend == "docker":
            await cleanup_orphans(sandbox_owner)
        addr = await server.start()
        logger.info("kama-core %s listening addr=%s", kama_claude.__version__, addr)
        logger.info("config summary: %s", config_summary(self._config))

        loop = asyncio.get_running_loop()
        shutdown = self._shutdown
        try:
            if sys.platform != "win32":
                # 用户按 Ctrl+C，或进程收到终止信号，就调用shutdown.set()，通知主协程停止等待
                loop.add_signal_handler(signal.SIGINT, shutdown.set)
                loop.add_signal_handler(signal.SIGTERM, shutdown.set)
            # Windows 由 asyncio.run() 将 Ctrl+C 转为主任务取消，再进入 finally 清理。
            # 如果 shutdown 未设置，await 会挂起当前协程，事件循环继续运行，服务器正常处理客户端请求
            # 直到某个信号触发 shutdown.set()，await 才返回，程序继续往下走
            await shutdown.wait()
        finally:
            # Windows系统下，用户按 Ctrl+C 时，asyncio.run() 会取消主任务，无须额外注册信号处理器
            logger.info("shutting down")
            server.begin_shutdown()
            try:
                try:
                    if self._sessions is not None:
                        await self._sessions.shutdown()
                finally:
                    if self._mcp_manager is not None:
                        await self._mcp_manager.stop_all()
            finally:
                try:
                    await server.stop()
                finally:
                    if self._trace is not None:
                        await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        pass
