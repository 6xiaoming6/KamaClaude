from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kama_claude.core.bus.events import RunStartedEvent, StepStartedEvent
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.sender import ConnectionSender


@pytest.fixture
async def delivery():
    senders = []

    def make(broadcaster, error=None):
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.drain = AsyncMock(side_effect=error)
        sender = ConnectionSender(writer, on_failure=lambda: broadcaster.unsubscribe(writer))
        senders.append(sender)
        return sender

    yield make
    await asyncio.gather(*(sender.close() for sender in senders))


def run_started(run_id="r1"):
    return RunStartedEvent(run_id=run_id, goal="test", ts="t")


# 广播快速返回，测试等待发送队列排空后检查真正写出的帧。
async def test_subscriber_receives_matching_event(delivery):
    broadcaster = IpcEventBroadcaster()
    sender = delivery(broadcaster)
    broadcaster.subscribe(sender, ["run.*"])
    await broadcaster.handle(run_started())
    await sender._queue.join()
    payload = json.loads(sender.writer.write.call_args[0][0])
    assert payload["kind"] == "event" and payload["event"]["type"] == "run.started"


async def test_no_subscription_no_write(delivery):
    broadcaster = IpcEventBroadcaster()
    sender = delivery(broadcaster)
    await broadcaster.handle(run_started())
    sender.writer.write.assert_not_called()


async def test_topic_glob_matches_step_not_run(delivery):
    broadcaster = IpcEventBroadcaster()
    sender = delivery(broadcaster)
    broadcaster.subscribe(sender, ["step.*"])
    await broadcaster.handle(StepStartedEvent(run_id="r1", step=1, ts="t"))
    await broadcaster.handle(run_started())
    await sender._queue.join()
    sender.writer.write.assert_called_once()
    assert json.loads(sender.writer.write.call_args[0][0])["event"]["type"] == "step.started"


@pytest.mark.parametrize("scope, expected", [("global", 2), ("run:r1", 1)])
async def test_scope_filters_run_ids(delivery, scope, expected):
    broadcaster = IpcEventBroadcaster()
    sender = delivery(broadcaster)
    broadcaster.subscribe(sender, ["run.*"], scope=scope)
    await broadcaster.handle(run_started())
    await broadcaster.handle(run_started("r2"))
    await sender._queue.join()
    assert sender.writer.write.call_count == expected


async def test_unsubscribe_stops_delivery_without_closing_sender(delivery):
    broadcaster = IpcEventBroadcaster()
    sender = delivery(broadcaster)
    broadcaster.subscribe(sender, ["run.*"])
    broadcaster.unsubscribe(sender.writer)
    await broadcaster.handle(run_started())
    assert not sender.closed
    await sender.send(b"rpc response\n")
    sender.writer.write.assert_called_once_with(b"rpc response\n")


async def test_dead_connection_removed_after_failure(delivery):
    broadcaster = IpcEventBroadcaster()
    sender = delivery(broadcaster, ConnectionResetError())
    broadcaster.subscribe(sender, ["run.*"])
    await broadcaster.handle(run_started())
    await sender._queue.join()
    assert not broadcaster._subscriptions and sender.closed
    sender.writer.write.reset_mock()
    await broadcaster.handle(run_started())
    sender.writer.write.assert_not_called()
