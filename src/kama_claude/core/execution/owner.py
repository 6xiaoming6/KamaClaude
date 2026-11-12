from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from kama_claude.core.execution.cli import DockerCli

log = logging.getLogger(__name__)
LABEL = "io.kamaclaude"


@dataclass(frozen=True)
class SandboxOwner:
    scope: str = "standalone"
    pid: int = field(default_factory=os.getpid)
    started: float = field(default_factory=lambda: psutil.Process().create_time())

    # 同一 Core 地址及会话目录共享归属，但不同活跃进程仍有独立身份。
    @classmethod
    def for_core(cls, sessions: Path, host: str, port: int) -> SandboxOwner:
        identity = f"{os.path.normcase(str(sessions.resolve()))}|{host}|{port}"
        return cls(scope=hashlib.sha256(identity.encode()).hexdigest()[:24])

    # 容器标签不含凭据，Run 与执行尝试分别标识。
    def labels(self, run_id: str, attempt: str) -> dict[str, str]:
        return {
            f"{LABEL}.scope": self.scope, f"{LABEL}.pid": str(self.pid),
            f"{LABEL}.started": str(self.started), f"{LABEL}.run": run_id,
            f"{LABEL}.attempt": attempt,
        }


# 仅删除有完整归属标签且原宿主进程已不存在的容器；不确定时保守跳过。
async def cleanup_orphans(owner: SandboxOwner, cli: DockerCli | None = None) -> None:
    cli = cli or DockerCli()
    try:
        # 留出 CLI 取消回收时间，不能耗尽现有 Core 的十五秒启动期限。
        async with asyncio.timeout(5):
            await cli.check_local()
            names = (await cli.checked(
                "ps", "-aq", "--filter", f"label={LABEL}.scope={owner.scope}",
            )).splitlines()
            for name in names:
                labels = json.loads(await cli.checked(
                    "inspect", "--format", "{{json .Config.Labels}}", name,
                ))
                if labels.get(f"{LABEL}.scope") != owner.scope:
                    continue
                try:
                    pid, started = int(labels[f"{LABEL}.pid"]), float(labels[f"{LABEL}.started"])
                    if pid <= 0 or not math.isfinite(started) or started <= 0:
                        continue
                    if abs(psutil.Process(pid).create_time() - started) < 0.01:
                        continue
                except psutil.NoSuchProcess:
                    pass
                except (psutil.AccessDenied, KeyError, TypeError, ValueError):
                    continue
                result = await cli.call("rm", "-f", name, timeout=5)
                if result.error or result.returncode:
                    log.warning("sandbox orphan cleanup failed container=%s", name)
                else:
                    log.info("removed orphan sandbox container=%s", name)
    except (OSError, RuntimeError, TimeoutError, ValueError):
        log.warning("sandbox orphan scan unavailable; containers were not claimed as cleaned")
