from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from kama_claude.core.config import ExecutionConfig
from kama_claude.core.execution.docker import DockerCommandExecutor
from kama_claude.core.execution.owner import LABEL, SandboxOwner, cleanup_orphans
from kama_claude.core.session.store import SessionStore
from tests.integration.test_s8_recovery import close, connect, wait_status

IMAGE = "kamaclaude-sandbox:py312-node24"


@pytest.fixture(scope="module", autouse=True)
# 功能：真实容器用例只在本机 Linux 引擎和显式构建的镜像可用时运行。
# 设计：只检查，不下载、不构建；缺少前置条件明确跳过，不冒充真实验证通过。
def docker_ready() -> None:
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                              capture_output=True, timeout=10, check=False)
        image = subprocess.run(["docker", "image", "inspect", IMAGE],
                               capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Docker sandbox prerequisites unavailable: {exc}")
    if info.returncode or info.stdout.strip() != b"linux" or image.returncode:
        pytest.skip("Requires local Docker Linux engine and built kamaclaude sandbox image")


# 功能：验证同容器保留文件和用户依赖，屏蔽凭据、根目录写入及未挂载宿主内容。
# 设计：真实 Docker 配置和实际脚本双重核对，工作区名称包含逗号及空格。
async def test_real_isolation_and_reuse(tmp_path: Path) -> None:
    root = tmp_path / "work, space"
    root.mkdir()
    for name in (".env", ".env.local", ".env.example"):
        (root / name).write_text("FAKE_CREDENTIAL", encoding="utf-8")
    (tmp_path / "outside-marker").write_text("outside", encoding="utf-8")
    executor = DockerCommandExecutor(root, ExecutionConfig())
    code = """import json, os
from pathlib import Path
Path('kept.txt').write_text('workspace')
Path('/home/kama/kept.txt').write_text('home')
Path('/tmp/kept.txt').write_text('tmp')
blocked = False
try:
    Path('/etc/sandbox-write').write_text('no')
except OSError:
    blocked = True
print(json.dumps(dict(uid=os.getuid(), env=Path('.env').read_text(), local=Path('.env.local').read_text(),
    example=Path('.env.example').read_text(), blocked=blocked, outside=Path('/outside-marker').exists(),
    socket=Path('/var/run/docker.sock').exists(), key='DEEPSEEK_API_KEY' in os.environ)))
"""
    try:
        result = await executor.execute("export TRANSIENT=yes; cd /workspace; python -c " + shlex.quote(code), 15)
        assert not result.error and result.returncode == 0, result
        data = json.loads(result.output)
        assert data == {"uid": data["uid"], "env": "", "local": "", "example": "FAKE_CREDENTIAL",
                        "blocked": True, "outside": False, "socket": False, "key": False}
        assert data["uid"] != 0
        result = await executor.execute(
            'test -z "$TRANSIENT" && test "$PWD" = /workspace && '
            'test -f kept.txt && test -f /home/kama/kept.txt && test -f /tmp/kept.txt && '
            'python --version && node --version && npm --version && pytest --version', 15,
        )
        assert not result.error and result.returncode == 0, result
        inspect = json.loads(await executor.cli.checked("inspect", executor.name))[0]
        settings = inspect["HostConfig"]
        assert settings["ReadonlyRootfs"] and settings["NetworkMode"] == "bridge"
        assert settings["Memory"] == settings["MemorySwap"] == 2048 * 1024**2
        assert settings["NanoCpus"] == 2_000_000_000 and settings["PidsLimit"] == 128
        assert settings["CapDrop"] == ["ALL"] and not settings["Privileged"]
        assert not settings["PortBindings"]
        assert (root / "kept.txt").read_text() == "workspace"
        assert (root / ".env").read_text() == "FAKE_CREDENTIAL"
    finally:
        assert await executor.aclose()
    assert (await executor.cli.call("inspect", executor.name, timeout=5)).returncode != 0


# 功能：验证真实联网可安装 Python 和 npm 依赖，安装结果在后续调用中仍可使用。
# 设计：只下载小型公开依赖到容器可写目录，不访问模型服务或传输项目代码。
async def test_real_network_and_user_dependencies(tmp_path: Path) -> None:
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig())
    try:
        result = await executor.execute(
            "python -m pip install --disable-pip-version-check --retries 0 --timeout 10 colorama==0.4.6 "
            "&& npm install --prefix /home/kama/node-test --ignore-scripts --no-audit --no-fund "
            "--fetch-retries=0 --fetch-timeout=10000 is-number@7.0.0", 60,
        )
        assert not result.error and result.returncode == 0, result
        result = await executor.execute(
            "python -c 'import colorama; assert colorama.__version__ == \"0.4.6\"' "
            "&& node -e 'if (!require(\"/home/kama/node-test/node_modules/is-number\")(42)) process.exit(1)'",
            10,
        )
        assert not result.error and result.returncode == 0, result
    finally:
        assert await executor.aclose()


@pytest.mark.parametrize("cancel", [False, True])
# 功能：验证真实命令超时或取消后容器被删除，工作区已有副作用保留供用户确认。
# 设计：等到容器写入可观察文件后再取消，确保覆盖实际执行而非启动前失败。
async def test_real_timeout_and_cancellation(tmp_path: Path, cancel: bool) -> None:
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig())
    task = asyncio.create_task(executor.execute("echo started > effect.txt; sleep 60", 60 if cancel else 1))
    try:
        if cancel:
            async with asyncio.timeout(15):
                while not (tmp_path / "effect.txt").exists():
                    await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.outcome_unknown and result.error_type == "timeout", result
        assert (tmp_path / "effect.txt").read_text().strip() == "started"
        assert (await executor.cli.call("inspect", executor.name, timeout=5)).returncode != 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await executor.aclose()


class DockerDaemon:
    # 所有进程与标签仅属于本测试临时目录，不操作其他 Core 的容器。
    def __init__(self, root: Path, port: int) -> None:
        self.root, self.port = root, port
        self.proc: subprocess.Popen[bytes] | None = None

    # 进程启动与 Docker 预检查可能较慢，使用明确的有界期限及诊断日志。
    async def start(self) -> None:
        (self.root / "ready.json").unlink(missing_ok=True)
        with (self.root / "daemon.log").open("ab") as log:
            self.proc = subprocess.Popen([
                sys.executable, "-X", "utf8", "-B", str(Path(__file__).with_name("docker_daemon.py")),
                str(self.root), str(self.port),
            ], stdout=log, stderr=log)
        async with asyncio.timeout(30):
            while not (self.root / "ready.json").exists():
                assert self.proc.poll() is None, (self.root / "daemon.log").read_text()
                await asyncio.sleep(0.02)

    # 硬退出绕过 Python finally，验证下一进程必须回收真实遗留容器。
    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.mark.parametrize("restart", [False, True])
# 功能：验证真实 TCP 断线不取消容器 Run，Core 硬重启后回收旧容器并从工具游标继续。
# 设计：同一回复两个 Bash 工具，以文件确认第二个已执行，再断线或杀掉真实进程。
async def test_real_core_disconnect_and_restart(tmp_path: Path, free_port: int, restart: bool) -> None:
    daemon = DockerDaemon(tmp_path, free_port)
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig())
    client = receiver = waiter = None
    owner = SandboxOwner.for_core(tmp_path / "sessions", "127.0.0.1", free_port)
    await daemon.start()
    try:
        client, receiver = await connect(free_port)
        sid = (await client.send_command("session.create", {"mode": "chat"}))["session_id"]
        await client.send_command("session.set_permission_mode", {"session_id": sid, "mode": "full"})
        waiter = asyncio.create_task(client.send_command(
            "session.send_message", {"session_id": sid, "content": "run two commands"},
        ))
        async with asyncio.timeout(30):
            while not (tmp_path / "b-started").exists():
                if waiter.done():
                    await waiter
                    pytest.fail("Run finished before the controlled second tool started")
                await asyncio.sleep(0.02)
        before = SessionStore(tmp_path / "sessions").checkpoints.load(sid)
        assert before.run.tool_index == 1 and before.run.tools[0].status == "completed"
        containers = (await executor.cli.checked(
            "ps", "-aq", "--filter", f"label={LABEL}.scope={owner.scope}",
        )).splitlines()
        assert len(containers) == 1
        if restart:
            daemon.kill()
        await close(client, receiver)
        client = receiver = None
        with suppress(ConnectionError):
            await waiter
        waiter = None
        if restart:
            await daemon.start()
            assert (await executor.cli.call("inspect", containers[0], timeout=5)).returncode != 0
        client, receiver = await connect(free_port)
        opened = await client.send_command("session.open", {"session_id": sid})
        if restart:
            assert opened["run"]["status"] == "interrupted"
            assert opened["run"]["tools"][1]["status"] == "unknown"
            await client.send_command("session.set_permission_mode", {"session_id": sid, "mode": "full"})
            (tmp_path / "release-b").touch()
            await client.send_command("session.resume", {
                "session_id": sid, "expected_revision": opened["revision"], "action": "retry_tool",
            })
        else:
            assert opened["run"]["status"] == "running"
            assert (await executor.cli.checked(
                "ps", "-aq", "--filter", f"label={LABEL}.scope={owner.scope}",
            )).splitlines() == containers
            (tmp_path / "release-b").touch()
        await wait_status(client, sid, "success")
        assert (tmp_path / "effects.txt").read_text().splitlines() == (["a", "b", "b"] if restart else ["a", "b"])
        # Run 终态先在 finish 时提交，等待 Runner 清理及终态通知前的 finally 完成。
        async with asyncio.timeout(10):
            while (await executor.cli.checked("ps", "-aq", "--filter", f"label={LABEL}.scope={owner.scope}")).strip():
                await asyncio.sleep(0.02)
    finally:
        if client is not None and receiver is not None:
            await close(client, receiver)
        if waiter is not None:
            with suppress(ConnectionError, asyncio.CancelledError):
                await asyncio.wait_for(waiter, 3)
        daemon.kill()
        await cleanup_orphans(owner)
