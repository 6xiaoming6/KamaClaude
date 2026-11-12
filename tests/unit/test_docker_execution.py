from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.bus.events import LogLineEvent
from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.config import DockerConfig, ExecutionConfig, KamaConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.events.bus import EventBus
from kama_claude.core.execution.cli import OUTPUT_LIMIT, DockerCli
from kama_claude.core.execution.docker import DockerCommandExecutor, container_env
from kama_claude.core.execution.model import execution_for_run, snapshot_execution
from kama_claude.core.execution.owner import LABEL, SandboxOwner, cleanup_orphans
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.permissions.bash import CommandRule
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.command import CommandResult
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from tests.unit.test_subagent_runtime import Provider, spawn_tool


class FakeCli(DockerCli):
    # 只替代外部 Docker 进程，执行器的锁、生命周期、配置和错误判断真实运行。
    def __init__(self) -> None:
        self.env = {}
        self.calls: list[tuple[str, ...]] = []
        self.failure = ""
        self.wait_on = ""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.labels: dict[str, dict[str, str]] = {}

    # 故障注入覆盖启动前、命令发出后和清理失败三个边界。
    async def call(self, *args: str, timeout: float = 30) -> CommandResult:
        self.calls.append(args)
        kind = args[0]
        if kind == self.wait_on:
            self.entered.set()
            await self.release.wait()
        if kind == self.failure:
            return CommandResult("partial", None, "injected failure", error_type="timeout")
        if kind == "image":
            return CommandResult("sha256:test-image", 0)
        if kind == "inspect":
            return CommandResult(json.dumps(self.labels[args[-1]]) if self.labels else "true", 0)
        if kind == "ps":
            return CommandResult("\n".join(self.labels), 0)
        return CommandResult("done", 0)

    # 本地性检查另有真实参数测试，这里记录调用但不访问 Docker 引擎。
    async def check_local(self) -> None:
        await self.checked("local")


# 功能：验证多次调用及并发请求只创建一次容器，最终只删除一次。
# 设计：检查实际 CLI 参数序列，覆盖并发懒创建而非只断言某个私有标志。
async def test_run_reuses_container_and_closes_idempotently(tmp_path: Path) -> None:
    cli = FakeCli()
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), run_id="r", cli=cli)
    results = await asyncio.gather(*(executor.execute(f"echo {n}", 5) for n in range(3)))
    assert all(r.returncode == 0 and not r.error for r in results)
    assert sum(args[0] == "create" for args in cli.calls) == 1
    assert sum(args[0] == "exec" for args in cli.calls) == 3
    assert await executor.aclose() and await executor.aclose()
    assert sum(args[0] == "rm" for args in cli.calls) == 1
    assert (await executor.execute("echo never", 5)).error_type == "configuration_error"


# 功能：验证挂载、非 root、只读根目录、资源和默认联网参数实际传给 Docker。
# 设计：包含空格和逗号的路径，按 Docker 的 CSV 挂载格式解析并核对凭据遮蔽。
async def test_container_parameters_and_secret_masks(tmp_path: Path) -> None:
    root = tmp_path / "work, space"
    root.mkdir()
    for name in (".env", ".env.local", ".env.example"):
        (root / name).write_text("USER_DATA", encoding="utf-8")
    cli = FakeCli()
    executor = DockerCommandExecutor(root, ExecutionConfig(), cli=cli)
    try:
        assert not (await executor.execute("true", 5)).error
        args = next(args for args in cli.calls if args[0] == "create")
        assert args[args.index("--network") + 1] == "bridge"
        assert args[args.index("--user") + 1].split(":")[0] != "0"
        for flag, value in (("--memory", "2048m"), ("--memory-swap", "2048m"),
                            ("--pids-limit", "128"), ("--cap-drop", "ALL")):
            assert args[args.index(flag) + 1] == value
        assert "--read-only" in args and "--privileged" not in args and "--publish" not in args
        mounts = [next(csv.reader([args[i + 1]])) for i, flag in enumerate(args) if flag == "--mount"]
        assert ["type=bind", f"source={root}", "target=/workspace"] in mounts
        for name in (".env", ".env.local"):
            mask = next(m for m in mounts if f"target=/workspace/{name}" in m)
            source = Path(next(field[7:] for field in mask if field.startswith("source=")))
            assert source.read_bytes() == b"" and "readonly" in mask
        assert not any("target=/workspace/.env.example" in m for m in mounts)
        assert all((root / name).read_text() == "USER_DATA" for name in (".env", ".env.local"))
    finally:
        await executor.aclose()


@pytest.mark.parametrize("stage", ["local", "image", "create", "start", "exec"])
# 功能：验证启动前错误为已知失败，用户命令发出后的故障为未知，均无本机回退。
# 设计：逐个边界注入 Docker 错误，检查是否实际出现 exec 及是否清理容器。
async def test_failure_boundaries(tmp_path: Path, stage: str) -> None:
    cli = FakeCli()
    cli.failure = stage
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=cli)
    result = await executor.execute("echo guarded", 1)
    assert bool(result.error) and result.outcome_unknown == (stage == "exec")
    assert any(args[0] == "exec" for args in cli.calls) == (stage == "exec")
    assert any(args[0] == "rm" for args in cli.calls) == (stage in {"create", "start", "exec"})
    assert await executor.aclose()


@pytest.mark.parametrize("stage", ["create", "exec"])
# 功能：验证取消发生在创建或执行期间时仍删除容器，取消异常继续传递。
# 设计：使用事件控制取消窗口，不靠 sleep 猜测进度。
async def test_cancellation_cleans_container(tmp_path: Path, stage: str) -> None:
    cli = FakeCli()
    cli.wait_on = stage
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=cli)
    task = asyncio.create_task(executor.execute("sleep 10", 20))
    await cli.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sum(args[0] == "rm" for args in cli.calls) == 1


# 功能：验证清理失败可见，不重复执行命令或伪称容器已删除。
# 设计：通过真实 EventBus 收集错误，重复关闭只返回已有失败结果。
async def test_cleanup_failure_notifies_without_reexecution(tmp_path: Path) -> None:
    cli, bus, messages = FakeCli(), EventBus(), []

    # 收集真实清理告警，避免仅测试 logger 调用次数。
    async def observe(event: Any) -> None:
        if isinstance(event, LogLineEvent):
            messages.append(event.message)

    bus.subscribe(observe)
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=cli, bus=bus)
    await executor.execute("true", 5)
    cli.failure = "rm"
    assert not await executor.aclose() and not await executor.aclose()
    assert messages and "cleanup incomplete" in messages[0]
    assert sum(args[0] == "exec" for args in cli.calls) == 1


# 功能：验证只有明确允许的变量进入容器，系统路径、模型凭据和 Docker 配置被排除。
# 设计：同时覆盖转发与显式值两个入口，检查不会泄漏未请求的环境变量。
def test_container_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-secret")
    monkeypatch.setenv("DOCKER_HOST", "fake-daemon")
    monkeypatch.setenv("APP_SETTING", "allowed")
    config = ExecutionConfig(
        forward_env=["DEEPSEEK_API_KEY", "DOCKER_HOST", "APP_SETTING", "PATH"],
        env={"OPENAI_API_KEY": "also-fake", "HOME": "bad", "EXPLICIT": "value"},
    )
    assert container_env(config) == {"APP_SETTING": "allowed", "EXPLICIT": "value"}


# 功能：验证默认后端为 Docker，TOML 和环境覆盖一致，非法额度明确拒绝。
# 设计：经过公开配置入口，不只检查 dataclass 的默认字段。
def test_execution_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = KamaConfig()
    assert config.execution.backend == "docker"
    apply_toml(config, {"execution": {"backend": "host", "docker": {"memory_mb": 4096}}})
    monkeypatch.setenv("KAMA_EXECUTION_BACKEND", "docker")
    monkeypatch.setenv("KAMA_DOCKER_CPUS", "1.5")
    apply_env(config)
    assert config.execution.backend == "docker" and config.execution.docker.cpus == 1.5
    assert config.execution.docker.memory_mb == 4096
    for values in ({"backend": "invalid"}, {"docker": {"memory_mb": True}},
                   {"docker": {"cpus": float("nan")}}, {"docker": {"pids_limit": 0}}):
        with pytest.raises(SystemExit):
            apply_toml(config, {"execution": values})


# 功能：验证检查点只保存非敏感配置，恢复不随当前后端变化，旧记录沿用 host。
# 设计：真实 JSON 序列化后恢复，覆盖默认值升级和秘密泄漏风险。
def test_execution_snapshot_is_frozen_and_has_no_secrets() -> None:
    config = ExecutionConfig(env={"APP_TOKEN": "fake-private"})
    run = RunCheckpoint(run_id="r", goal="g", model="fake", workspace=".", max_steps=1,
                        execution=snapshot_execution(config))
    payload = run.model_dump_json()
    assert "fake-private" not in payload and "APP_TOKEN" not in payload
    restored = RunCheckpoint.model_validate_json(payload)
    changed = replace(config, backend="host", docker=DockerConfig(memory_mb=4096))
    assert execution_for_run(restored, changed).backend == "docker"
    assert execution_for_run(restored, changed).docker.memory_mb == 2048
    restored.execution = None
    assert execution_for_run(restored, config).backend == "host"


# 功能：验证清理只针对本 Core 已退出的进程，活跃与其他归属容器不会被删。
# 设计：构造完整标签并模拟 PID 复用，核对实际 rm 的目标。
async def test_orphan_cleanup_checks_process_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    owner, cli = SandboxOwner(scope="my-core"), FakeCli()
    cli.labels = {
        "live": owner.labels("r1", "a1"),
        "old": {**owner.labels("r2", "a2"), f"{LABEL}.started": "1.0"},
        "other": {**owner.labels("r3", "a3"), f"{LABEL}.scope": "different-core"},
        "unknown": {f"{LABEL}.scope": "my-core"},
    }
    await cleanup_orphans(owner, cli)
    assert [args[-1] for args in cli.calls if args[0] == "rm"] == ["old"]


# 功能：验证输出超过上限后仍读到 EOF，保留内容不会无限增长。
# 设计：直接用真实 asyncio 流输入百万字节，不依赖 Docker 是否安装。
async def test_cli_output_is_bounded() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"a" * 1_000_000)
    stream.feed_eof()
    kept = bytearray()
    await DockerCli()._read(stream, kept)
    assert len(kept) == OUTPUT_LIMIT + 1
    assert DockerCli._decode(kept).endswith("[truncated]")


# 功能：验证 Docker 容器中的 Bash 使用 Bash 规则审批，即使宿主是 Windows。
# 设计：通过真实 invoke_tool 和审批 Future 执行包含引号的允许命令。
async def test_docker_approval_uses_actual_shell(tmp_path: Path) -> None:
    cli, bus, registry = FakeCli(), EventBus(), ToolRegistry()
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=cli)
    registry.register(BashTool(tmp_path, executor=executor))
    permissions = PermissionManager({"bash": ToolPolicy(
        PermissionDecision.ASK, [CommandRule(("echo", "hello world"), "allow")],
    )})
    result = await invoke_tool(
        registry, ToolCallBlock("t", "bash", {"command": "echo 'hello world'"}), bus, "r",
        permission_manager=permissions, session_id="s",
    )
    assert not result.is_error and permissions.pending_for_session("s") == []
    await executor.aclose()


# 功能：验证普通 Runner 的两个 Bash 调用共用容器，成功和异常退出都释放资源。
# 设计：只替代 Docker CLI，模型、工具注册、Loop 和 Runner finally 都实际执行。
@pytest.mark.parametrize("failure", [False, True])
async def test_runner_owns_container_lifetime(tmp_path: Path, monkeypatch, failure: bool) -> None:
    monkeypatch.chdir(tmp_path)
    cli = FakeCli()
    if failure:
        cli.failure = "exec"
    monkeypatch.setattr("kama_claude.core.execution.docker.DockerCli", lambda: cli)
    calls = [LlmResponse("tool_use", tool_calls=[ToolCallBlock(str(i), "bash", {"command": "true"})])
             for i in range(2)]
    model = Provider(*calls)
    outcome = await AgentRunner(KamaConfig(), provider=model).run_and_capture("run commands")
    assert outcome.status == ("interrupted" if failure else "success")
    assert sum(args[0] == "create" for args in cli.calls) == 1
    assert sum(args[0] == "rm" for args in cli.calls) == 1
    assert sum(args[0] == "exec" for args in cli.calls) == (1 if failure else 2)


# 功能：验证没有 Bash 的任务完全不访问 Docker，子任务独立持有容器并收尾。
# 设计：共享 CLI 日志观察实际容器创建，而不是模拟 Runner 的清理函数。
async def test_lazy_runner_and_child_lifetimes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli = FakeCli()
    monkeypatch.setattr("kama_claude.core.execution.docker.DockerCli", lambda: cli)
    assert (await AgentRunner(KamaConfig(), provider=Provider()).run_and_capture("answer")).status == "success"
    assert cli.calls == []
    model = Provider(LlmResponse("tool_use", tool_calls=[ToolCallBlock("t", "bash", {"command": "true"})]))
    parent = spawn_tool(tmp_path, model)
    assert not (await parent.invoke({"description": "execute", "prompt": "run", "subagent_type": "executor"})).is_error
    assert sum(args[0] == "create" for args in cli.calls) == 1
    assert sum(args[0] == "rm" for args in cli.calls) == 1


# 功能：验证工具结果保存失败立即停止下一命令，并在异常退出时删除容器。
# 设计：通过真实 SessionStore 注入已完成工具的提交失败，不绕过 Loop 保存边界。
async def test_checkpoint_failure_closes_sandbox_before_next_operation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli = FakeCli()
    monkeypatch.setattr("kama_claude.core.execution.docker.DockerCli", lambda: cli)
    model = Provider(LlmResponse("tool_use", tool_calls=[
        ToolCallBlock("a", "bash", {"command": "true"}),
        ToolCallBlock("b", "bash", {"command": "true"}),
    ]))
    store, bus = SessionStore(tmp_path / "sessions"), EventBus()
    manager = SessionManager(store, lambda: AgentRunner(KamaConfig(), provider=model, bus=bus), bus)
    session = await manager.create("chat")
    original = store.checkpoints.save

    # 只有业务已返回并准备提交结果时才失败，确保容器确实创建过。
    def fail_completed(checkpoint):
        if checkpoint.run and any(t.status == "completed" for t in checkpoint.run.tools):
            raise CheckpointError("disk full")
        original(checkpoint)

    monkeypatch.setattr(store.checkpoints, "save", fail_completed)
    try:
        with pytest.raises(CheckpointError):
            await manager.send_message(session.id, "run two commands")
        assert sum(args[0] == "exec" for args in cli.calls) == 1
        assert sum(args[0] == "rm" for args in cli.calls) == 1
        assert len(model.calls) == 1
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("mode", list(PermissionMode))
# 功能：验证任何审批模式都不能越过 Bash 明确禁止规则，拒绝前不启动容器。
# 设计：走实际工具入口和权限管理器，而非只单测解析函数。
async def test_docker_denial_precedes_container_creation(tmp_path: Path, mode: PermissionMode) -> None:
    cli, registry = FakeCli(), ToolRegistry()
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=cli)
    registry.register(BashTool(tmp_path, executor=executor))
    permissions = PermissionManager({"bash": ToolPolicy(
        PermissionDecision.ASK, [CommandRule(("touch", "blocked"), "deny")],
    )})
    permissions.set_mode("s", mode)
    result = await invoke_tool(
        registry, ToolCallBlock("t", "bash", {"command": "touch blocked"}), EventBus(), "r",
        permission_manager=permissions, session_id="s",
    )
    assert result.error_type == "permission_denied" and cli.calls == []
    await executor.aclose()


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://remote", "http://remote"])
# 功能：验证远程或非支持的 Docker 连接在检查阶段拒绝，不执行挂载或业务命令。
# 设计：直接使用真实本地性验证，替身仅记录 CLI 调用。
async def test_remote_daemon_is_rejected(endpoint: str) -> None:
    cli = FakeCli()
    cli.env = {"DOCKER_HOST": endpoint}
    with pytest.raises(RuntimeError, match="local Docker"):
        await DockerCli.check_local(cli)
    assert cli.calls == []


# 功能：验证容器初始化出错也会关闭本轮执行组件，不留下开放资源。
# 设计：在真实 Runner 的装配位置注入失败，观察注入执行器的关闭结果。
async def test_runner_initialization_failure_closes_executor(tmp_path: Path, monkeypatch) -> None:
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=FakeCli())
    runner = AgentRunner(KamaConfig(), provider=Provider(), runs_dir=tmp_path / "runs")
    monkeypatch.setattr("kama_claude.core.runner.create_executor", lambda *args, **kwargs: executor)

    # 模拟 Provider 或冻结 Schema 校验阶段失败，此时还没有任何 Bash 调用。
    def fail_build(*args, **kwargs):
        raise RuntimeError("assembly failed")

    monkeypatch.setattr(runner, "_build_loop", fail_build)
    assert (await runner.run_and_capture("task")).status == "failed"
    assert executor._closed and executor.cli.calls == []


# 功能：验证等待审批期间新加的禁止规则仍按真实 Bash 语法复核。
# 设计：引号参数在 cmd 与 Bash 中的解析不同，批准后禁止应阻止容器创建。
async def test_approval_recheck_uses_same_shell(tmp_path: Path) -> None:
    cli, bus, registry = FakeCli(), EventBus(), ToolRegistry()
    executor = DockerCommandExecutor(tmp_path, ExecutionConfig(), cli=cli)
    registry.register(BashTool(tmp_path, executor=executor))
    policy = ToolPolicy(PermissionDecision.ASK)
    permissions = PermissionManager({"bash": policy})

    # 用户回答前收紧规则，防止批准绕过新的禁止。
    async def respond(event: Any) -> None:
        if getattr(event, "type", "") == "permission.requested":
            assert "Docker Linux Bash" in event.reason
            policy.command_rules.append(CommandRule(("echo", "hello world"), "deny"))
            permissions.respond(event.request_id, "allow_once", session_id="s", run_id="r")

    bus.subscribe(respond)
    result = await invoke_tool(
        registry, ToolCallBlock("t", "bash", {"command": "echo 'hello world'"}), bus, "r",
        permission_manager=permissions, session_id="s",
    )
    assert result.error_type == "permission_denied" and cli.calls == []
    await executor.aclose()


# 功能：验证父子 Run 拥有不同容器，子容器先销毁，父容器保留到自己的 Run 结束。
# 设计：同一个真实 Runner 派生前台 executor，按 Run ID 决定固定模型响应。
async def test_parent_child_containers_are_distinct(tmp_path: Path, monkeypatch) -> None:
    from tests.unit.completion_support import submit_response

    monkeypatch.chdir(tmp_path)
    cli = FakeCli()
    monkeypatch.setattr("kama_claude.core.execution.docker.DockerCli", lambda: cli)

    class TreeProvider:
        # 按本轮 Run 身份推进，主子任务都实际调用各自的 Bash 工具。
        async def chat(self, **kwargs):
            root = kwargs["run_id"] == "root"
            if kwargs["step"] == 1:
                response = LlmResponse("tool_use", tool_calls=[ToolCallBlock("b", "bash", {"command": "true"})])
            elif root and kwargs["step"] == 2:
                response = LlmResponse("tool_use", tool_calls=[ToolCallBlock("s", "spawn_agent", {
                    "description": "child work", "prompt": "run a command", "subagent_type": "executor",
                })])
            else:
                response = LlmResponse("end_turn", text="done")
            return submit_response(response, kwargs["tool_schemas"], kwargs["run_id"], kwargs["step"])

    result = await AgentRunner(KamaConfig(), provider=TreeProvider()).run_and_capture("work", run_id="root")
    assert result.status == "success"
    names = [args[args.index("--name") + 1] for args in cli.calls if args[0] == "create"]
    assert len(names) == len(set(names)) == 2
    assert [args[-1] for args in cli.calls if args[0] == "rm"] == names[::-1]
