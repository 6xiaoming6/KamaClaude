from __future__ import annotations

import asyncio

from kama_claude.cli.commands import chat
from kama_claude.core.config import KamaConfig
from tests.unit.test_permission_tui import approval


async def test_chat_approves_while_run_response_is_pending(monkeypatch, capsys) -> None:
    requested, approved = asyncio.Event(), asyncio.Event()
    calls = []

    class Client:
        async def connect(self):
            pass
        def on_event(self, handler):
            self.handler = handler
        async def run_event_loop(self):
            await asyncio.Future()
        async def close(self):
            pass
        async def send_command(self, method, params):
            calls.append((method, params))
            if method == "session.create":
                return {"session_id": "sess-one"}
            if method == "session.send_message":
                await self.handler(approval())
                requested.set()
                await approved.wait()
            if method == "permission.respond":
                approved.set()
            return {"ok": True}

    counter = 0
    async def readline(prompt):
        nonlocal counter
        counter += 1
        if counter == 1:
            return "execute goal"
        if counter == 2:
            await asyncio.wait_for(requested.wait(), 1)
            return "y"
        await asyncio.wait_for(approved.wait(), 1)
        raise EOFError

    monkeypatch.setattr(chat, "SocketClient", lambda *args: Client())
    monkeypatch.setattr(chat, "_readline", readline)
    assert await asyncio.wait_for(chat._chat_async(KamaConfig()), 3) == 0
    response = next(params for method, params in calls if method == "permission.respond")
    assert response == {
        "request_id": "approval-1", "session_id": "sess-one", "run_id": "child-a",
        "decision": "allow_once",
    }
    subscription = next(params for method, params in calls if method == "event.subscribe")
    assert subscription["scope"] == "session:sess-one"
    assert "[last-command]" in capsys.readouterr().out


async def test_chat_printer_tracks_concurrent_requests_and_expiration(capsys) -> None:
    printer = chat.ChatPrinter()
    await printer.handle(approval("a"))
    await printer.handle(approval("b", "child-b"))
    assert capsys.readouterr().out.count("[permission a]") == 2
    await printer.handle({"type": "permission.denied", "request_id": "b"})
    assert list(printer.pending_permissions) == ["a"]
