from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from kama_claude.core.bus.envelope import SERVER_BUSY, SERVER_SHUTTING_DOWN, JsonRpcRequest
from kama_claude.core.transport.sender import ConnectionSender
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer


@asynccontextmanager
async def running(**limits):
    server = SocketServer("127.0.0.1", 0, **limits)
    await server.start()
    clients = []

    async def connect():
        client = SocketClient("127.0.0.1", server._server.sockets[0].getsockname()[1])
        await client.connect()
        receiver = asyncio.create_task(client.run_event_loop())
        clients.append((client, receiver))
        return client

    try:
        yield server, connect
    finally:
        for client, _ in clients:
            await client.close()
        await asyncio.gather(*(receiver for _, receiver in clients), return_exceptions=True)
        await server.stop()


async def test_full_ordinary_capacity_does_not_block_approval_or_ping():
    async with running(max_requests=1) as (server, connect):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def slow(params):
            calls.append(params)
            started.set()
            await release.wait()
            return {"done": True}

        async def ping(params):
            return {"pong": True}

        async def approve(params):
            release.set()
            return {"approved": True}

        server.register("slow", slow)
        server.register("core.ping", ping, control=True)
        server.register("permission.respond", approve, control=True)
        client = await connect()
        held = asyncio.create_task(client.send_command("slow", {}))
        await started.wait()
        try:
            rejected = await asyncio.gather(*(
                client.send_command("slow", {"i": index}) for index in range(10)
            ), return_exceptions=True)
            assert all(isinstance(error, IpcError) and error.code == SERVER_BUSY for error in rejected)
            assert len(calls) == 1 and server._normal_active == 1
            assert await client.send_command("core.ping", {}) == {"pong": True}
            assert await client.send_command("permission.respond", {}) == {"approved": True}
            assert await asyncio.wait_for(held, 1) == {"done": True}
            await asyncio.sleep(0)
            assert server._normal_active == server._control_active == 0
        finally:
            release.set()
            await asyncio.gather(held, return_exceptions=True)


async def test_global_limit_is_shared_by_connections_and_released():
    async with running(global_max_requests=2) as (server, connect):
        release, both_started = asyncio.Event(), asyncio.Event()
        calls = 0

        async def slow(params):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await release.wait()
            return {"ok": True}

        async def ping(params):
            return {"pong": True}

        server.register("slow", slow)
        server.register("core.ping", ping, control=True)
        first, second, third = await connect(), await connect(), await connect()
        holds = [asyncio.create_task(client.send_command("slow", {})) for client in (first, second)]
        try:
            await asyncio.wait_for(both_started.wait(), 1)
            with pytest.raises(IpcError) as exc:
                await third.send_command("slow", {})
            assert exc.value.code == SERVER_BUSY and calls == 2
            assert await third.send_command("core.ping", {}) == {"pong": True}
        finally:
            release.set()
            await asyncio.gather(*holds, return_exceptions=True)
        assert await third.send_command("slow", {}) == {"ok": True}


@pytest.mark.parametrize("limits", [{"max_control_requests": 1}, {"global_max_control_requests": 1}])
async def test_control_requests_have_their_own_bounded_capacity(limits):
    async with running(**limits) as (server, connect):
        release, started = asyncio.Event(), asyncio.Event()

        async def waiting(params):
            started.set()
            await release.wait()
            return {}

        async def fast(params):
            return {"ok": True}

        server.register("permission.respond", waiting, control=True)
        server.register("core.ping", fast, control=True)
        server.register("ordinary", fast)
        first = await connect()
        target = await connect() if "global_max_control_requests" in limits else first
        held = asyncio.create_task(first.send_command("permission.respond", {}))
        try:
            await started.wait()
            with pytest.raises(IpcError) as exc:
                await target.send_command("core.ping", {})
            assert exc.value.code == SERVER_BUSY
            assert await target.send_command("ordinary", {}) == {"ok": True}
        finally:
            release.set()
            await held


async def test_shutdown_gate_rejects_new_work_but_keeps_control_on_existing_connection():
    async with running() as (server, connect):
        executed = []

        async def handler(params):
            executed.append(params)
            return {"ok": True}

        server.register("agent.run", handler)
        server.register("permission.respond", handler, control=True)
        client = await connect()
        assert await client.send_command("agent.run", {}) == {"ok": True}
        server.begin_shutdown()
        with pytest.raises(IpcError) as exc:
            await client.send_command("agent.run", {})
        assert exc.value.code == SERVER_SHUTTING_DOWN and len(executed) == 1
        assert await client.send_command("permission.respond", {}) == {"ok": True}


async def test_stop_cancels_and_reclaims_requests_and_senders():
    async with running(shutdown_timeout=0.1) as (server, connect):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def waiting(params):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        server.register("wait", waiting)
        client = await connect()
        pending = asyncio.create_task(client.send_command("wait", {}))
        await started.wait()
        senders = list(server._senders.values())
        await asyncio.wait_for(server.stop(), 1)
        await server.stop()
        result = (await asyncio.gather(pending, return_exceptions=True))[0]
        assert isinstance(result, ConnectionError) and cancelled.is_set()
        assert not server._senders and not server._requests and not server._connections
        assert server._normal_active == server._control_active == 0
        assert all(sender._task.done() for sender in senders)


async def test_handler_error_releases_capacity():
    async with running(max_requests=1) as (server, connect):
        async def fail(params):
            raise RuntimeError("controlled failure")

        server.register("fail", fail)
        client = await connect()
        for _ in range(2):
            with pytest.raises(IpcError) as exc:
                await client.send_command("fail", {})
            assert exc.value.code == -32603
        await asyncio.sleep(0)
        assert server._normal_active == 0


async def test_received_but_not_started_request_cannot_cross_shutdown_gate():
    from tests.unit.test_connection_sender import Writer

    server = SocketServer("127.0.0.1", 0)
    writer = Writer(blocked=False)
    sender = ConnectionSender(writer)
    server._senders[writer] = sender
    server._requests[writer] = {}
    executed = []

    async def work(params):
        executed.append(params)
        return {}

    server.register("work", work)
    server._admit(JsonRpcRequest(id="r", method="work"), writer)
    pending = list(server._requests[writer])
    server.begin_shutdown()
    await asyncio.gather(*pending)
    assert not executed and server._normal_active == 0
    assert b"Server shutting down" in writer.output[0]
    await sender.close()


async def test_response_wait_remains_inside_request_quota():
    from tests.unit.test_connection_sender import Writer

    server = SocketServer("127.0.0.1", 0, max_requests=1)
    writer = Writer()
    sender = ConnectionSender(writer)
    server._senders[writer] = sender
    server._requests[writer] = {}

    async def work(params):
        return {}

    server.register("work", work)
    server._admit(JsonRpcRequest(id="first", method="work"), writer)
    await writer.entered.wait()
    assert server._normal_active == 1
    rejected = server._admit(JsonRpcRequest(id="second", method="work"), writer)
    assert rejected.error.code == SERVER_BUSY
    writer.release.set()
    await asyncio.gather(*list(server._requests[writer]))
    assert server._normal_active == 0
    await sender.close()


async def test_closed_sender_cannot_launch_buffered_or_not_started_requests():
    from tests.unit.test_connection_sender import Writer

    server = SocketServer("127.0.0.1", 0)
    writer = Writer(blocked=False)
    sender = ConnectionSender(writer)
    server._senders[writer] = sender
    server._requests[writer] = {}
    executed = []

    async def work(params):
        executed.append(params)
        return {}

    server.register("work", work)
    server._admit(JsonRpcRequest(id="first", method="work"), writer)
    pending = list(server._requests[writer])
    sender.abort(ConnectionError("client closed"))
    reader = asyncio.StreamReader()
    reader.feed_data(JsonRpcRequest(id="buffered", method="work").model_dump_json().encode() + b"\n")
    await server._read_loop(reader, writer)
    await asyncio.gather(*pending)
    assert not executed and server._normal_active == 0
    await sender.close()
