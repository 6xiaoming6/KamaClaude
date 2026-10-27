from __future__ import annotations

import asyncio
from pathlib import Path

from kama_claude.core.trace.record import TraceRecord


# 将trace写进文件比较慢，如果产生一条record就写一条会非常慢
# 所以外部调用 emit 方法把 record 写进内存队列中，
# 再由后台 task 调用 _drain 方法持续读取队列并写入文件。
class TraceWriter:
    # 初始化 TraceWriter；写入目标文件路径在 start() 前不会创建
    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # 创建目录、启动后台 drain task
    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())

    # 等待队列清空后取消 drain task
    async def stop(self) -> None:
        # join() 会阻塞等待，直到队列中每一个被 get() 的项都调用了 task_done()
        # 靠这个机制来保证"退出前把所有积压的记录写完"
        await self._queue.join()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # 非阻塞地将 record 放入写入队列
    def emit(self, record: TraceRecord) -> None:
        self._queue.put_nowait(record)

    # 持续从队列读取 record 并追加写入文件
    async def _drain(self) -> None:
        with open(self._path, "a") as f:
            while True:
                # 队列为空时，await queue.get() 会让当前协程挂起；
                # emit 塞进新记录后，事件循环会唤醒这个协程，get() 返回。
                record = await self._queue.get()
                try:
                    f.write(record.model_dump_json() + "\n")
                    f.flush()
                finally:
                    self._queue.task_done()
