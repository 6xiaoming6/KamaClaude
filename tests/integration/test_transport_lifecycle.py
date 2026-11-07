from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import kama_claude.core.app as core_app
from kama_claude.core.app import CoreApp
from kama_claude.core.bus.envelope import SERVER_SHUTTING_DOWN, HandlerError
from kama_claude.core.bus.events import SessionCreatedEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.sender import ConnectionSender
from kama_claude.core.transport.socket_client import SocketClient
from kama_claude.core.transport.socket_server import SocketServer
from tests.unit.test_checkpoint_recovery import Provider
from tests.unit.test_connection_sender import Writer


# 普通 RPC 等待 Run 占满额度时，停止控制仍可用；工具完成后两个 RPC 都正常返回。
async def test_graceful_interrupt_works_while_send_request_occupies_slot(tmp_path):
    from tests.unit.test_checkpoint_recovery import manager, tool_response
    from tests.unit.test_session_interrupt import BlockingTool

    store = SessionStore(tmp_path)
    probe = BlockingTool()
    bus = EventBus()
    sessions = manager(store, Provider([tool_response("a")]), [probe], bus)
    session = await sessions.create("chat")
    broadcaster = IpcEventBroadcaster()
    bus.subscribe(broadcaster.handle)
    app = CoreApp()
    app._sessions, app._broadcaster = sessions, broadcaster
    server = SocketServer("127.0.0.1", 0, broadcaster, max_requests=1)
    server.register("session.send_message", app._session_send_handler)
    server.register("session.interrupt", app._session_interrupt_handler, control=True)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    client = SocketClient("127.0.0.1", port)
    await client.connect()
    receiver = asyncio.create_task(client.run_event_loop())
    sent = asyncio.create_task(client.send_command("session.send_message", {
        "session_id": session.id, "content": "goal",
    }))
    try:
        await asyncio.wait_for(probe.started.wait(), 2)
        cp = store.checkpoints.load(session.id)
        result = await asyncio.wait_for(client.send_command("session.interrupt", {
            "session_id": session.id, "run_id": cp.run.run_id,
        }), 2)
        assert result["status"] == "stopping" and not sent.done()
        assert not probe.cancelled
        probe.release.set()
        accepted = await asyncio.wait_for(sent, 2)
        assert accepted["run_id"] == cp.run.run_id
        stopped = store.checkpoints.load(session.id)
        assert stopped.run.reason == "user_stopped"
        assert stopped.run.tools[0].status == "completed"
    finally:
        probe.release.set()
        await sessions.shutdown()
        await client.close()
        await asyncio.gather(receiver, sent, return_exceptions=True)
        await server.stop()


# 真实 Core 协调层、TCP、检查点和 Loop；只有模型回复由事件控制。
async def test_rpc_disconnect_reclaims_request_but_core_run_continues(tmp_path: Path):
    started, release = asyncio.Event(), asyncio.Event()

    class Model(Provider):
        async def chat(self, **kwargs):
            started.set()
            await release.wait()
            return await super().chat(**kwargs)

    bus, store = EventBus(), SessionStore(tmp_path)
    model = Model([LlmResponse("end_turn", text="done")])
    manager = SessionManager(
        store, lambda: AgentRunner(KamaConfig(), provider=model, bus=bus), bus,
    )
    session = await manager.create("chat")
    broadcaster = IpcEventBroadcaster()
    bus.subscribe(broadcaster.handle)
    app = CoreApp()
    app._sessions, app._broadcaster = manager, broadcaster
    server = SocketServer("127.0.0.1", 0, broadcaster, max_requests=1)
    server.register("session.send_message", app._session_send_handler)
    server.register("session.open", app._session_open_handler)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    first, second = SocketClient("127.0.0.1", port), SocketClient("127.0.0.1", port)
    await first.connect()
    receiver = asyncio.create_task(first.run_event_loop())
    request = asyncio.create_task(first.send_command("session.send_message", {
        "session_id": session.id, "content": "controlled goal",
    }))
    second_receiver = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        connections = list(server._connections)
        await first.close()
        await receiver
        await asyncio.wait_for(asyncio.gather(*connections), 2)
        assert isinstance((await asyncio.gather(request, return_exceptions=True))[0], ConnectionError)
        assert server._normal_active == 0 and manager.is_running(session.id)
        await second.connect()
        second_receiver = asyncio.create_task(second.run_event_loop())
        opened = await second.send_command("session.open", {"session_id": session.id})
        assert opened["running"] and opened["run"]["status"] == "running"
        release.set()
        await asyncio.wait_for(asyncio.shield(manager._tasks[session.id]), 2)
        final = await second.send_command("session.open", {"session_id": session.id})
        assert final["run"]["status"] == "success" and final["run"]["result"] == "done"
        assert len(model.calls) == 1
    finally:
        release.set()
        await manager.shutdown()
        await first.close()
        await second.close()
        await asyncio.gather(receiver, request, return_exceptions=True)
        if second_receiver:
            await second_receiver
        await server.stop()


# 一帧额度也能完成大量回放，证明回放按写出节奏推进而不是瞬间填满队列。
async def test_replay_paces_output_and_precedes_rpc_response(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    events = [{"type": "run.started", "run_id": "r", "goal": str(index)} for index in range(300)]
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    monkeypatch.setattr(core_app, "events_file", lambda _: path)
    writer = Writer(blocked=False)
    sender = ConnectionSender(writer, max_frames=1)
    count = await CoreApp()._replay_events("r", sender, ["run.*"])
    await sender.send(b'{"jsonrpc":"2.0","id":"subscribe","result":{}}\n')
    await sender.close()
    payloads = [json.loads(frame) for frame in writer.output]
    assert count == 300 and [frame["event"] for frame in payloads[:-1]] == events
    assert payloads[-1]["id"] == "subscribe" and sender._dropped == 0


# agent.run 在创建会话时让出调度权，关闭后不能再登记后续 Run。
async def test_shutdown_blocks_run_after_request_already_started_session_creation(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    bus = EventBus()

    async def pause_creation(event):
        if isinstance(event, SessionCreatedEvent):
            entered.set()
            await release.wait()

    bus.subscribe(pause_creation)
    runner_calls = []

    def runner():
        runner_calls.append(True)
        return AgentRunner(KamaConfig(), bus=bus)

    manager = SessionManager(SessionStore(tmp_path), runner, bus)
    app = CoreApp()
    app._sessions = manager
    request = asyncio.create_task(app._agent_run_handler({"goal": "controlled goal"}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await manager.shutdown()
        release.set()
        with pytest.raises(HandlerError) as error:
            await asyncio.wait_for(request, 1)
        assert error.value.code == SERVER_SHUTTING_DOWN
        assert not runner_calls and not manager._tasks
        sid = next(iter(manager._sessions))
        with pytest.raises(HandlerError) as resume_error:
            await manager.resume(sid, 0)
        assert resume_error.value.code == SERVER_SHUTTING_DOWN
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await manager.shutdown()
