from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest

from kama_claude.core.bus.envelope import MESSAGE_TOO_LARGE, HandlerError, JsonRpcSuccess
from kama_claude.core.bus.events import RunStartedEvent
from kama_claude.core.transport import ipc_broadcaster, socket_client, socket_server
from kama_claude.core.transport.framing import FrameTooLargeError, encode_frame
from kama_claude.core.transport.sender import ConnectionSender
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer


@pytest.fixture
def small_frames(monkeypatch):
    monkeypatch.setattr(socket_client, "_MAX_LINE_BYTES", 256)
    monkeypatch.setattr(socket_server, "_MAX_LINE_BYTES", 256)


async def start_server() -> tuple[SocketServer, int]:
    server = SocketServer("127.0.0.1", 0)

    async def ping(params):
        return {"ok": True}

    server.register("ping", ping)
    await server.start()
    return server, server._server.sockets[0].getsockname()[1]


@pytest.mark.parametrize("newline", [True, False])
async def test_server_rejects_oversized_frame_and_still_serves_other_clients(small_frames, newline):
    server, port = await start_server()
    errors = []
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, error: errors.append(error))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x" * 300 + (b"\n" if newline else b""))
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert response["error"]["message"] == "Request too large"
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
        await writer.wait_closed()
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        receiver = asyncio.create_task(client.run_event_loop())
        try:
            assert await asyncio.wait_for(client.send_command("ping", {}), 2) == {"ok": True}
        finally:
            await client.close()
            await receiver
        assert not errors
    finally:
        await server.stop()
        loop.set_exception_handler(original)


@pytest.mark.parametrize("error_response", [False, True])
async def test_server_returns_correlated_error_for_oversized_response(small_frames, error_response):
    server, port = await start_server()

    async def large(params):
        if error_response:
            raise HandlerError(-32100, "中" * 300)
        return {"data": "中" * 300}

    server.register("large", large)
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    receiver = asyncio.create_task(client.run_event_loop())
    try:
        with pytest.raises(IpcError) as exc:
            await asyncio.wait_for(client.send_command("large", {}), 2)
        assert exc.value.code == MESSAGE_TOO_LARGE
        assert await asyncio.wait_for(client.send_command("ping", {}), 2) == {"ok": True}
        assert not client._pending
    finally:
        await client.close()
        await receiver
        await server.stop()


async def test_local_oversized_request_does_not_send_or_break_connection(small_frames):
    server, port = await start_server()
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    receiver = asyncio.create_task(client.run_event_loop())
    try:
        with pytest.raises(IpcError) as exc:
            await client.send_command("ping", {"data": "中" * 100})
        assert exc.value.code == MESSAGE_TOO_LARGE and not client._pending
        assert await asyncio.wait_for(client.send_command("ping", {}), 2) == {"ok": True}
    finally:
        await client.close()
        await receiver
        await server.stop()


@pytest.mark.parametrize("newline", [True, False])
async def test_client_fails_all_waiters_on_oversized_response_without_waiting_for_eof(
    small_frames, newline,
):
    disconnected = asyncio.Event()

    async def handle(reader, writer):
        try:
            requests = [json.loads(await reader.readline()) for _ in range(2)]
            payload = json.dumps({
                "jsonrpc": "2.0", "id": requests[0]["id"], "result": {"data": "x" * 500},
            }).encode()
            writer.write(payload + (b"\n" if newline else b""))
            await writer.drain()
            # 服务端保持连接，必须由客户端识别超限并结束等待。
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        client = SocketClient("127.0.0.1", server.sockets[0].getsockname()[1])
        await client.connect()
        receiver = asyncio.create_task(client.run_event_loop())
        results = await asyncio.wait_for(asyncio.gather(
            client.send_command("one", {}), client.send_command("two", {}),
            return_exceptions=True,
        ), 2)
        assert all(isinstance(result, FrameTooLargeError) for result in results)
        await asyncio.wait_for(receiver, 2)
        await asyncio.wait_for(disconnected.wait(), 2)
        assert not client._pending and client._writer is None


async def test_oversized_push_closes_only_matching_subscriber(monkeypatch):
    monkeypatch.setattr(ipc_broadcaster, "encode_frame", lambda msg: encode_frame(msg, 256))
    broadcaster = ipc_broadcaster.IpcEventBroadcaster()
    writer = Mock(spec=asyncio.StreamWriter)
    other = Mock(spec=asyncio.StreamWriter)
    sender = ConnectionSender(writer, on_failure=lambda: broadcaster.unsubscribe(writer))
    other_sender = ConnectionSender(other)
    broadcaster.subscribe(sender, ["run.*"])
    broadcaster.subscribe(other_sender, ["tool.*"])
    await broadcaster.handle(RunStartedEvent(run_id="r", goal="x" * 300, ts="t"))
    writer.close.assert_called_once_with()
    writer.write.assert_not_called()
    assert [sub.sender.writer for sub in broadcaster._subscriptions] == [other]
    await sender.close()
    await other_sender.close()


def test_frame_limit_counts_encoded_utf8_bytes():
    response = JsonRpcSuccess(id="r", result="中" * 80)
    assert len(response.model_dump_json()) < 256
    with pytest.raises(FrameTooLargeError):
        encode_frame(response, 256)


async def test_unrepresentable_response_id_closes_connection_without_unhandled_exception(small_frames):
    server = SocketServer("127.0.0.1", 0)
    writer = Mock(spec=asyncio.StreamWriter)
    sender = ConnectionSender(writer)
    server._senders[writer] = sender
    await server._send(writer, JsonRpcSuccess(id="x" * 300, result={}))
    await sender.close()
    writer.close.assert_called_once_with()
    writer.write.assert_not_called()
