from __future__ import annotations

import asyncio

import pytest

from kama_claude.core.bus.events import RunStartedEvent
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.sender import ConnectionSender


class Writer:
    def __init__(self, *, blocked=True, error=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.output = []
        self.error = error
        self.closes = 0
        if not blocked:
            self.release.set()

    def write(self, data):
        self.output.append(data)

    async def drain(self):
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise self.error

    def close(self):
        self.closes += 1


async def start_send(sender, data):
    entered = asyncio.Event()

    async def send():
        entered.set()
        await sender.send(data)

    task = asyncio.create_task(send())
    await entered.wait()
    return task


# 当前帧也占用额度，RPC、广播与后续响应按同一队列顺序写出。
async def test_fifo_order_and_inflight_accounting():
    writer = Writer()
    sender = ConnectionSender(writer)
    first = await start_send(sender, b"reply\n")
    await writer.entered.wait()
    sender.enqueue(b"event\n")
    last = await start_send(sender, b"last\n")
    assert sender._frames == 3 and sender._bytes == len(b"reply\nevent\nlast\n")
    writer.release.set()
    await asyncio.wait_for(asyncio.gather(first, last), 1)
    await sender.close()
    assert writer.output == [b"reply\n", b"event\n", b"last\n"]
    assert sender._frames == sender._bytes == 0 and writer.closes == 1


@pytest.mark.parametrize("limits", [{"max_frames": 2}, {"max_bytes": 4}])
async def test_queue_overflow_fails_all_waiters_without_waiting_for_drain(limits):
    writer = Writer()
    sender = ConnectionSender(writer, **limits)
    first = await start_send(sender, b"aa")
    await writer.entered.wait()
    second = await start_send(sender, b"bb")
    with pytest.raises(ConnectionError, match="queue limit"):
        sender.enqueue(b"x")
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
    assert all(isinstance(result, ConnectionError) for result in results)
    await sender.close()
    assert sender._task.done() and sender._frames == sender._bytes == 0
    assert sender._dropped == 2 and writer.closes == 1


@pytest.mark.parametrize("error", [None, ConnectionResetError("peer reset")])
async def test_timeout_or_network_failure_closes_sender_and_resolves_rpc(error):
    writer = Writer(blocked=error is None, error=error)
    sender = ConnectionSender(writer, drain_timeout=0.01)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(sender.send(b"reply"), 1)
    await sender.close()
    assert sender.closed and sender._task.done() and sender._frames == 0


async def test_slow_client_does_not_block_publisher_or_healthy_client():
    broadcaster = IpcEventBroadcaster()
    slow_writer, fast_writer = Writer(), Writer(blocked=False)
    slow = ConnectionSender(slow_writer, max_frames=1,
                            on_failure=lambda: broadcaster.unsubscribe(slow_writer))
    fast = ConnectionSender(fast_writer)
    broadcaster.subscribe(slow, ["*"])
    broadcaster.subscribe(fast, ["*"])
    event = RunStartedEvent(run_id="r", goal="goal", ts="t")
    try:
        await asyncio.wait_for(broadcaster.handle(event), 0.5)
        await slow_writer.entered.wait()
        await asyncio.wait_for(broadcaster.handle(event), 0.5)
        await asyncio.wait_for(fast._queue.join(), 1)
        assert slow.closed and not fast.closed and len(fast_writer.output) == 2
        assert all(sub.sender is fast for sub in broadcaster._subscriptions)
    finally:
        await asyncio.gather(slow.close(), fast.close())


async def test_close_is_bounded_and_idempotent_with_pending_output():
    writer = Writer()
    sender = ConnectionSender(writer)
    sending = await start_send(sender, b"reply")
    await writer.entered.wait()
    await asyncio.wait_for(sender.close(timeout=0.01), 0.5)
    with pytest.raises(ConnectionError):
        await sending
    await sender.close()
    assert sender._task.done() and sender._queue.empty() and writer.closes == 1


async def test_cancelling_close_still_fails_pending_sends():
    writer = Writer()
    sender = ConnectionSender(writer)
    sending = await start_send(sender, b"reply")
    await writer.entered.wait()
    closing = asyncio.create_task(sender.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(sending, 0.5)
    assert sender._task.done() and sender._frames == 0


async def test_cancelling_one_send_does_not_cancel_sender():
    writer = Writer()
    sender = ConnectionSender(writer)
    sending = await start_send(sender, b"first")
    await writer.entered.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    writer.release.set()
    await sender.send(b"second")
    await sender.close()
    assert writer.output == [b"first", b"second"]


async def test_burst_of_events_gives_healthy_sender_a_chance_to_run():
    writer = Writer(blocked=False)
    sender = ConnectionSender(writer, max_frames=4)
    broadcaster = IpcEventBroadcaster()
    broadcaster.subscribe(sender, ["*"])
    try:
        for index in range(300):
            await broadcaster.handle(RunStartedEvent(run_id="r", goal=str(index), ts="t"))
        await asyncio.wait_for(sender._queue.join(), 1)
        assert not sender.closed and len(writer.output) == 300 and sender._dropped == 0
    finally:
        await sender.close()
