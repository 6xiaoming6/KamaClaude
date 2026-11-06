from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    MESSAGE_TOO_LARGE,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_BUSY,
    SERVER_SHUTTING_DOWN,
    HandlerError,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)
from kama_claude.core.checkpoint.checkpoint import CheckpointError
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.framing import MAX_LINE_BYTES, FrameTooLargeError, encode_frame
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.sender import ConnectionSender

logger = logging.getLogger(__name__)
type CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]
_sender_var: ContextVar[ConnectionSender] = ContextVar("_sender_var")
_MAX_LINE_BYTES = MAX_LINE_BYTES


def _now() -> str:
    return datetime.now(UTC).isoformat()


# handler 只引用已有连接，不能为订阅或回放另外创建发送任务。
def get_connection_sender() -> ConnectionSender:
    return _sender_var.get()


def get_connection_writer() -> asyncio.StreamWriter:
    return get_connection_sender().writer


class SocketServer:
    def __init__(
        self, host: str, port: int, broadcaster: IpcEventBroadcaster | None = None,
        trace: TraceWriter | None = None, *, max_requests: int = 8,
        max_control_requests: int = 2, global_max_requests: int = 64,
        global_max_control_requests: int = 8, send_timeout: float = 5.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            max_requests, max_control_requests, global_max_requests, global_max_control_requests,
        )) or send_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("transport limits must be positive")
        self._host, self._port = host, port
        self._handlers: dict[str, CommandHandler] = {}
        self._controls: set[str] = set()
        self._server: asyncio.AbstractServer | None = None
        self._broadcaster, self._trace = broadcaster, trace
        self._senders: dict[asyncio.StreamWriter, ConnectionSender] = {}
        self._requests: dict[asyncio.StreamWriter, dict[asyncio.Task[None], bool]] = {}
        self._connections: set[asyncio.Task[None]] = set()
        self._max_requests, self._max_controls = max_requests, max_control_requests
        self._global_max_requests = global_max_requests
        self._global_max_controls = global_max_control_requests
        self._normal_active = self._control_active = 0
        self._send_timeout, self._shutdown_timeout = send_timeout, shutdown_timeout
        self._closing = False
        self._stop_lock = asyncio.Lock()

    def register(self, method: str, handler: CommandHandler, *, control: bool = False) -> None:
        self._handlers[method] = handler
        if control:
            self._controls.add(method)
        else:
            self._controls.discard(method)

    async def start(self) -> str:
        try:
            _reader, writer = await asyncio.open_connection(self._host, self._port)
            writer.close()
            await writer.wait_closed()
            raise SystemExit(f"core already running at {self._host}:{self._port}")
        except (ConnectionRefusedError, OSError):
            pass
        self._closing = False
        self._server = await asyncio.start_server(
            self._handle_connection, host=self._host, port=self._port, limit=_MAX_LINE_BYTES,
        )
        return f"{self._host}:{self._port}"

    # 先关业务接入；已有连接仍能回答审批，直到业务检查点收尾完成。
    def begin_shutdown(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()

    async def stop(self) -> None:
        async with self._stop_lock:
            self.begin_shutdown()
            writers = list(self._requests)
            try:
                await asyncio.gather(*(self._cancel_requests(writer) for writer in writers))
            finally:
                try:
                    await asyncio.gather(*(
                        sender.close(self._shutdown_timeout)
                        for sender in list(self._senders.values())
                    ))
                finally:
                    await self._wait_tasks(set(self._connections), "connection cleanup")
                    if self._server is not None:
                        try:
                            async with asyncio.timeout(self._shutdown_timeout):
                                await self._server.wait_closed()
                        except TimeoutError:
                            logger.warning("TCP server close timed out")

    # 只取消 RPC 等待；SessionManager 持有并 shield 的业务 Run 不属于此集合。
    async def _cancel_requests(self, writer: asyncio.StreamWriter) -> None:
        tasks = set(self._requests.get(writer, {}))
        for task in tasks:
            task.cancel()
        await self._wait_tasks(tasks, "request cancellation")

    async def _wait_tasks(self, tasks: set[asyncio.Task[None]], label: str) -> None:
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
        if pending:
            logger.warning("%s timed out; remaining=%d", label, len(pending))
            for task in pending:
                task.cancel()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        if self._closing:
            writer.close()
            return
        owner = asyncio.current_task()
        if owner is not None:
            self._connections.add(owner)

        def failed() -> None:
            if self._broadcaster is not None:
                self._broadcaster.unsubscribe(writer)

        sender = ConnectionSender(writer, drain_timeout=self._send_timeout, on_failure=failed)
        self._senders[writer] = sender
        self._requests[writer] = {}
        try:
            await self._read_loop(reader, writer)
        except ConnectionError as exc:
            logger.debug("client connection lost: %s", exc)
        finally:
            failed()
            try:
                await self._cancel_requests(writer)
            finally:
                try:
                    await sender.close(self._shutdown_timeout)
                finally:
                    try:
                        async with asyncio.timeout(self._shutdown_timeout):
                            await writer.wait_closed()
                    except (OSError, TimeoutError):
                        pass
                    self._senders.pop(writer, None)
                    # 不配合取消的请求仍保留登记，直到真正结束后释放全局额度。
                    if not self._requests.get(writer):
                        self._requests.pop(writer, None)
                    if owner is not None:
                        self._connections.discard(owner)

    # 校验和容量检查在 create_task 前完成，不堆积等待信号量的协程。
    async def _read_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sender = self._senders[writer]
        while not sender.closed:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                await self._send(writer, make_error(None, INVALID_REQUEST, "Request too large"))
                return
            if not line or sender.closed:
                return
            try:
                raw: Any = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                await self._send(writer, make_error(None, PARSE_ERROR, f"Parse error: {exc}"))
                continue
            try:
                request = JsonRpcRequest.model_validate(raw)
            except ValidationError as exc:
                await self._send(
                    writer, make_error(None, INVALID_REQUEST, "Invalid Request", str(exc)),
                )
                continue
            error = self._admit(request, writer)
            if error is not None:
                await self._send(writer, error)

    # 单事件循环内同步占位；完成回调复用原连接集合，迟到回调也能释放全局额度。
    def _admit(self, request: JsonRpcRequest, writer: asyncio.StreamWriter) -> JsonRpcError | None:
        control = request.method in self._controls
        if self._closing and not control:
            return make_error(request.id, SERVER_SHUTTING_DOWN, "Server shutting down")
        group = self._requests[writer]
        local = sum(flag == control for flag in group.values())
        active = self._control_active if control else self._normal_active
        if local >= (self._max_controls if control else self._max_requests) or active >= (
            self._global_max_controls if control else self._global_max_requests
        ):
            return make_error(request.id, SERVER_BUSY, "Server busy")
        task = asyncio.create_task(
            self._handle_request(request, writer), name=f"rpc:{request.method}",
        )
        group[task] = control
        if control:
            self._control_active += 1
        else:
            self._normal_active += 1

        def done(completed: asyncio.Task[None]) -> None:
            flag = group.pop(completed, None)
            if flag is not None:
                if flag:
                    self._control_active -= 1
                else:
                    self._normal_active -= 1
            if not group and writer not in self._senders:
                self._requests.pop(writer, None)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logger.error("request task failed: %s", error)

        task.add_done_callback(done)
        return None

    async def _handle_request(self, request: JsonRpcRequest, writer: asyncio.StreamWriter) -> None:
        sender = self._senders[writer]
        if sender.closed:
            return
        token = _sender_var.set(sender)
        try:
            await self._execute_request(request, writer)
        finally:
            _sender_var.reset(token)

    # handler 的业务异常仍转换为结构化响应；取消与检查点错误保留各自边界。
    async def _execute_request(self, req: JsonRpcRequest, writer: asyncio.StreamWriter) -> None:
        # 已接收但尚未调度的普通请求，也不能越过关闭接入的边界。
        if self._closing and req.method not in self._controls:
            await self._send(
                writer, make_error(req.id, SERVER_SHUTTING_DOWN, "Server shutting down"),
            )
            return
        if self._trace is not None:
            self._trace.emit(TraceRecord(
                ts=_now(), direction="CLIENT→CORE", layer="ipc", kind="command",
                client_id=str(writer.get_extra_info("peername", "<unknown>")),
                data={"method": req.method, "id": req.id, "params": req.params},
            ))
        handler = self._handlers.get(req.method)
        if handler is None:
            await self._send(
                writer, make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}"),
            )
            return
        try:
            result = await handler(req.params)
        except CheckpointError as exc:
            response: BaseModel = make_error(req.id, -32032, str(exc))
        except HandlerError as exc:
            response = make_error(req.id, exc.code, str(exc), exc.data)
        except ValidationError as exc:
            response = make_error(req.id, INVALID_REQUEST, "Invalid params", str(exc))
        except Exception:
            logger.exception("handler %s raised", req.method)
            response = make_error(req.id, INTERNAL_ERROR, "Internal error")
        else:
            data = result.model_dump() if isinstance(result, BaseModel) else result
            response = JsonRpcSuccess(id=req.id, result=data)
        await self._send(writer, response)

    async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
        sender = self._senders.get(writer)
        if sender is None:
            return
        try:
            frame = encode_frame(msg, _MAX_LINE_BYTES)
        except FrameTooLargeError:
            msg = make_error(getattr(msg, "id", None), MESSAGE_TOO_LARGE,
                             "Response too large; read data in smaller pages")
            try:
                frame = encode_frame(msg, _MAX_LINE_BYTES)
            except FrameTooLargeError as exc:
                sender.abort(exc)
                return
        try:
            await sender.send(frame)
        except ConnectionError:
            return
        if self._trace is not None:
            self._trace.emit(TraceRecord(
                ts=_now(), direction="CORE→CLIENT", layer="ipc",
                kind="error" if isinstance(msg, JsonRpcError) else "response",
                client_id=str(writer.get_extra_info("peername", "<unknown>")),
                data={**msg.model_dump(), "stage": "written"},
            ))
