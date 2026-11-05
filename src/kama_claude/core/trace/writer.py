from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kama_claude.core.trace.record import TraceRecord

log = logging.getLogger(__name__)


class TraceWriter:
    # Trace 是观察记录，写入失败不能阻塞 Core 或代替检查点的持久化责任。
    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._accepting = True
        self._closed = False
        self._failure: Exception | None = None
        self._dropped = 0
        self._stop_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None or self._closed:
            raise RuntimeError("trace writer already started or closed")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._failure = exc
            self._accepting = False
            self._discard_pending()
            log.exception("cannot start trace writer path=%s", self._path)
            return
        self._task = asyncio.create_task(self._drain())

    # 同时观察排空和后台任务；任务死亡时不能只等永远不会完成的 queue.join。
    async def stop(self, timeout_s: float = 5.0) -> None:
        async with self._stop_lock:
            if self._closed:
                return
            self._accepting = False
            drained = asyncio.create_task(self._queue.join())
            try:
                if self._task is not None:
                    done, _ = await asyncio.wait(
                        (drained, self._task), timeout=timeout_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        log.warning("trace shutdown timed out path=%s", self._path)
            finally:
                drained.cancel()
                if self._task is not None:
                    self._task.cancel()
                await asyncio.gather(
                    drained, *([self._task] if self._task is not None else []),
                    return_exceptions=True,
                )
                self._discard_pending()
                self._closed = True
                if self._dropped:
                    log.warning("trace discarded %d records path=%s", self._dropped, self._path)

    def emit(self, record: TraceRecord) -> None:
        if not self._accepting:
            self._dropped += 1
            if self._dropped == 1:
                log.warning(
                    "trace writer unavailable; records will be discarded path=%s", self._path,
                )
            return
        self._queue.put_nowait(record)

    def _discard_pending(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped += 1

    # 正常写入保留顺序；失败后消费剩余队列的计数，防止退出挂住。
    async def _drain(self) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as stream:
                while True:
                    record = await self._queue.get()
                    try:
                        stream.write(record.model_dump_json() + "\n")
                        stream.flush()
                    except Exception:
                        self._dropped += 1
                        raise
                    finally:
                        self._queue.task_done()
                    # 积压很多记录时也让出事件循环，使有界退出和其他请求及时调度。
                    await asyncio.sleep(0)
        except Exception as exc:
            self._failure = exc
            self._accepting = False
            self._discard_pending()
            log.exception("trace writer failed path=%s", self._path)
