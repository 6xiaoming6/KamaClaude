from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from kama_claude.core.transport.framing import MAX_LINE_BYTES

log = logging.getLogger(__name__)


@dataclass
class _Frame:
    data: bytes
    completion: asyncio.Future[None] | None = None


class ConnectionSender:
    # 每条连接只有一个写任务；积压额度包括已取出、仍在 drain 中的帧。
    def __init__(
        self, writer: asyncio.StreamWriter, *, max_frames: int = 128,
        max_bytes: int = MAX_LINE_BYTES + 1, drain_timeout: float = 5.0,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        if max_frames <= 0 or max_bytes <= 0 or drain_timeout <= 0:
            raise ValueError("sender limits must be positive")
        self.writer = writer
        self._max_frames, self._max_bytes = max_frames, max_bytes
        self._drain_timeout = drain_timeout
        self._on_failure = on_failure
        self._queue: asyncio.Queue[_Frame] = asyncio.Queue()
        self._frames = 0
        self._bytes = 0
        self._current: _Frame | None = None
        self._failure: ConnectionError | None = None
        self._accepting = True
        self._writer_closed = False
        self._dropped = 0
        self._close_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._run(), name="ipc-sender")
        self._task.add_done_callback(self._observe)

    @property
    def closed(self) -> bool:
        return not self._accepting

    @staticmethod
    def _observe(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    # 广播只负责入队，绝不等待网络或空闲位置。
    def enqueue(self, data: bytes) -> None:
        self._put(_Frame(data))

    # RPC 与回放等待这帧实际写出；等待者取消不影响其他帧的顺序。
    async def send(self, data: bytes) -> None:
        completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        try:
            self._put(_Frame(data, completion))
            await completion
        finally:
            if not completion.done():
                completion.cancel()
            elif not completion.cancelled():
                completion.exception()

    def _put(self, frame: _Frame) -> None:
        if self.closed:
            raise self._failure or ConnectionError("connection sender is closing")
        if self._frames >= self._max_frames or self._bytes + len(frame.data) > self._max_bytes:
            error = ConnectionError("client send queue limit exceeded")
            self.abort(error)
            raise error
        self._frames += 1
        self._bytes += len(frame.data)
        self._queue.put_nowait(frame)

    def _close_writer(self) -> None:
        if not self._writer_closed:
            self._writer_closed = True
            with suppress(OSError):
                self.writer.close()

    # 失败立即断开并唤醒全部发送等待者；丢弃帧数用于排障，不冒充已送达。
    def abort(self, error: ConnectionError) -> None:
        if self._failure is not None:
            return
        self._failure, self._accepting = error, False
        if self._current is not None:
            self._dropped += 1
            self._fail_frame(self._current, error)
        while not self._queue.empty():
            frame = self._queue.get_nowait()
            self._fail_frame(frame, error)
            self._release(frame)
            self._dropped += 1
        self._close_writer()
        if self._on_failure is not None:
            try:
                self._on_failure()
            except Exception:
                log.exception("cannot clean up failed connection subscription")
        if self._task is not asyncio.current_task():
            self._task.cancel()
        log.warning("IPC sender closed: %s; discarded=%d", error, self._dropped)

    @staticmethod
    def _fail_frame(frame: _Frame, error: ConnectionError) -> None:
        if frame.completion is not None and not frame.completion.done():
            frame.completion.set_exception(error)

    def _release(self, frame: _Frame) -> None:
        self._frames -= 1
        self._bytes -= len(frame.data)
        self._queue.task_done()

    async def _run(self) -> None:
        while True:
            frame = await self._queue.get()
            self._current = frame
            try:
                self.writer.write(frame.data)
                async with asyncio.timeout(self._drain_timeout):
                    await self.writer.drain()
                if frame.completion is not None and not frame.completion.done():
                    frame.completion.set_result(None)
            except asyncio.CancelledError:
                if self._accepting:
                    self.abort(ConnectionError("connection sender cancelled"))
                raise
            except Exception as exc:
                reason = "client send timed out" if isinstance(exc, TimeoutError) else str(exc)
                self.abort(ConnectionError(reason))
                return
            finally:
                self._current = None
                self._release(frame)

    # 正常关闭先尝试排空；超时主动丢弃并失败等待，不能无限卡住 Core 退出。
    async def close(self, timeout: float = 2.0) -> None:
        async with self._close_lock:
            self._accepting = False
            try:
                async with asyncio.timeout(timeout):
                    await self._queue.join()
            except TimeoutError:
                self.abort(ConnectionError("sender shutdown timed out"))
            except asyncio.CancelledError:
                self.abort(ConnectionError("sender shutdown cancelled"))
                raise
            finally:
                self._task.cancel()
                done, pending = await asyncio.wait({self._task}, timeout=timeout)
                if pending:
                    log.warning("IPC sender did not finish cancellation")
                    self._task.cancel()
                for task in done:
                    self._observe(task)
                self._close_writer()
