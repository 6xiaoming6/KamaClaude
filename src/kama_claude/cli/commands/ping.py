from __future__ import annotations

import asyncio
import sys
import time

from kama_claude.cli.rpc import ping
from kama_claude.core.config import KamaConfig
from kama_claude.core.transport.socket_client import IpcError


def cmd_ping(config: KamaConfig) -> None:
    try:
        asyncio.run(_ping(config))
    except (IpcError, OSError, TimeoutError, ValueError) as exc:
        print(f"error: ping {config.host}:{config.port}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _ping(config: KamaConfig) -> None:
    started = time.monotonic()
    pong = await ping(config)
    elapsed = int((time.monotonic() - started) * 1000)
    print(f"pong server={pong.server_version} uptime={pong.uptime_ms}ms latency={elapsed}ms")
