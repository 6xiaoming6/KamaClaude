from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest

from kama_claude.core.app import CoreApp
from kama_claude.core.events.bus import EventBus
from kama_claude.core.session import store as store_module
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.transport import socket_client, socket_server
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.core.transport.socket_server import SocketServer
from kama_claude.tui.app import KamaTuiApp


async def history(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_HISTORY_PAGE_BYTES", 512)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, Mock(), EventBus())
    session = await manager.create("chat")
    for index in range(12):
        store.append_message(session.id, "user" if index % 2 == 0 else "assistant",
                             f"{index}:" + "中" * 80, run_id=f"run-{index // 2}")
    return store, manager, session.id


async def test_pages_preserve_complete_unicode_messages_and_exclude_later_appends(tmp_path, monkeypatch):
    store, manager, sid = await history(tmp_path, monkeypatch)
    expected = store.read_history(sid, include_metadata=True)
    opened = await manager.open(sid)
    assert len(opened["messages"]) < len(expected) and opened["next_cursor"] is not None
    result = opened["messages"]
    end, cursor = opened["history_end"], opened["next_cursor"]
    store.append_message(sid, "user", "after snapshot", run_id="next")
    path = store.session_dir(sid) / "thread.jsonl"
    unchanged = path.read_bytes()
    while cursor is not None:
        page, following, actual_end = manager.get_history_page(sid, cursor, end, include_metadata=True)
        assert actual_end == end and (following is None or following > cursor)
        result.extend(page)
        cursor = following
    assert result == expected
    assert len({message["id"] for message in result}) == len(result)
    assert path.read_bytes() == unchanged
    assert store.read_history(sid)[-1]["content"] == "after snapshot"


async def test_page_boundaries_and_single_large_record(tmp_path, monkeypatch):
    store, _, sid = await history(tmp_path, monkeypatch)
    monkeypatch.setattr(store_module, "_HISTORY_PAGE_MESSAGES", 2)
    monkeypatch.setattr(store_module, "_HISTORY_PAGE_BYTES", 100_000)
    page, cursor, end = store.read_history_page(sid)
    assert len(page) == 2 and cursor is not None
    assert store.read_history_page(sid, end, end) == ([], None, end)
    for bad_cursor in (1, end + 1):
        with pytest.raises(ValueError):
            store.read_history_page(sid, bad_cursor, end)
    monkeypatch.setattr(store_module, "_HISTORY_PAGE_BYTES", 10)
    page, cursor, _ = store.read_history_page(sid)
    assert len(page) == 1 and page[0] == store.read_history(sid)[0]
    assert cursor is not None


async def test_full_history_larger_than_frame_can_be_read_over_real_tcp(tmp_path, monkeypatch):
    store, manager, sid = await history(tmp_path, monkeypatch)
    monkeypatch.setattr(socket_client, "_MAX_LINE_BYTES", 2048)
    monkeypatch.setattr(socket_server, "_MAX_LINE_BYTES", 2048)
    assert (store.session_dir(sid) / "thread.jsonl").stat().st_size > 2048
    app = CoreApp()
    app._sessions, app._broadcaster = manager, IpcEventBroadcaster()
    server = SocketServer("127.0.0.1", 0, app._broadcaster)
    server.register("session.open", app._session_open_handler)
    server.register("session.get_history", app._session_history_handler)
    await server.start()
    client = SocketClient("127.0.0.1", server._server.sockets[0].getsockname()[1])
    await client.connect()
    receiver = asyncio.create_task(client.run_event_loop())
    try:
        opened = await asyncio.wait_for(client.send_command("session.open", {"session_id": sid}), 2)
        result, cursor = opened["messages"], opened["next_cursor"]
        assert cursor is not None
        while cursor is not None:
            page = await asyncio.wait_for(client.send_command("session.get_history", {
                "session_id": sid, "cursor": cursor, "history_end": opened["history_end"],
                "include_metadata": True,
            }), 2)
            result.extend(page["messages"])
            cursor = page["next_cursor"]
        assert result == store.read_history(sid, include_metadata=True)
        with pytest.raises(IpcError) as exc:
            await asyncio.wait_for(client.send_command("session.get_history", {
                "session_id": sid, "cursor": 1, "history_end": opened["history_end"],
            }), 2)
        assert exc.value.code == -32602
    finally:
        await client.close()
        await receiver
        await server.stop()


async def test_tui_loads_all_pages_before_replaying_buffered_events(monkeypatch):
    async def idle(self):
        await asyncio.Future()

    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle)
    app = KamaTuiApp("127.0.0.1", 1)
    calls = []
    first = {"role": "assistant", "run_id": "old", "content": [{
        "type": "tool_use", "id": "t", "name": "read_file", "input": {"path": "a.py"},
    }]}
    second = {"role": "user", "run_id": "old", "content": [{
        "type": "tool_result", "tool_use_id": "t", "content": "full original output",
    }]}

    class Client:
        async def send_command(self, method, params):
            calls.append((method, params))
            if method == "session.open":
                return {
                    "session": {"id": "s", "status": "waiting_for_input"},
                    "revision": 4, "event_seq": 10, "messages": [deepcopy(first)],
                    "run": None, "running": False, "pending_permissions": [],
                    "next_cursor": 100, "history_end": 200,
                }
            assert app._opening
            app._event_buffer.append({
                "type": "run.finished", "session_id": "s", "event_seq": 11,
                "run_id": "live", "status": "success", "result": "new answer", "steps": 1,
            })
            return {"messages": [deepcopy(second)], "next_cursor": None, "history_end": 200}

    async with app.run_test() as pilot:
        app._client = Client()
        await app._open_session("s")
        await pilot.pause()
        assert calls[1] == ("session.get_history", {
            "session_id": "s", "cursor": 100, "history_end": 200, "include_metadata": True,
        })
        assert ("old", "t") in app._view.completed_tools
        assert not app._view.pending_tools
        assert "live" in app._view.final_replies and app._last_event_seq == 11
        assert not app._opening and not app._event_buffer
