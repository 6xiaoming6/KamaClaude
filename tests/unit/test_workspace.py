from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.events import PermissionRequestedEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.runner import AgentRunner
from kama_claude.core.subagent.tool import SpawnAgentTool
from kama_claude.core.tools.base import FileTool, ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.list_dir import ListDirTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.workspace import WorkspacePathError, WorkspacePaths


# 创建实际目录链接；Windows 使用不要求管理员权限的 junction
def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
        )
        if result.returncode:
            pytest.skip(f"cannot create junction: {result.stderr!r}")
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"cannot create directory symlink: {exc}")


# 删除链接本身，不递归访问它指向的目录
def unlink_directory(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()


# 在真实调用入口执行文件工具，统一收集审批事件和持久化边界
async def call_file(
    tool: FileTool, params: dict[str, Any], manager: PermissionManager | None = None,
    bus: EventBus | None = None, uid: str = "tool-one", stages: list[str] | None = None,
) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    return await invoke_tool(
        registry, ToolCallBlock(id=uid, name=tool.name, input=params), bus or EventBus(),
        "run-one", permission_manager=manager, session_id="session-one",
        checkpoint_hook=stages.append if stages is not None else None,
    )


# 功能：验证规范化后内部路径被接受，前缀相似目录仍判为外部
# 设计：混合相对、绝对和 .. 路径，直接断言规范目标而非字符串前缀
def test_canonical_boundary(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    paths = WorkspacePaths(root)
    for raw in ("a.txt", "nested/../a.txt", str(root / "a.txt")):
        access = paths.inspect(raw)
        assert access.target == root / "a.txt"
        assert not access.outside_workspace
    assert paths.inspect(str(tmp_path / "repo-other" / "a.txt")).outside_workspace
    assert paths.inspect("../outside.txt").outside_workspace


# 功能：验证独立工具捕获构造时的工作区，cwd 改变不影响文件和命令
# 设计：先构造工具再切目录，用真实读取和 shell 的 pwd/cd 检查执行位置
async def test_tools_capture_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, other = tmp_path / "root", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "file.txt").write_text("original", encoding="utf-8")
    monkeypatch.chdir(root)
    reader, bash = ReadFileTool(), BashTool()
    monkeypatch.chdir(other)
    assert (await reader.invoke({"path": "file.txt"})).content.endswith("1 | original")
    result = await bash.invoke({"command": "cd" if os.name == "nt" else "pwd"})
    assert str(root) in result.content


# 功能：验证直接调用及伪造模型授权参数都不能越过边界
# 设计：使用真正外部文件和同名 approved_path 输入，排除只在 invoker 拦截的漏洞
async def test_direct_and_forged_access_denied(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    tool = ReadFileTool(root)
    with pytest.raises(WorkspacePathError):
        await tool.invoke({"path": str(target)})
    with pytest.raises(ValidationError):
        await tool.invoke({"path": str(target), "approved_path": str(target)})
    result = await call_file(tool, {"path": str(target), "approved_path": str(target)})
    assert result.error_type == "schema_error"
    assert not result.outcome_unknown


# 功能：验证无效路径被明确拒绝且不会记录工具执行意图
# 设计：空串及 NUL 不应进入文件访问，也不能被误认为执行后的未知结果
@pytest.mark.parametrize("raw", ["", "\0bad"])
async def test_invalid_paths_denied_before_execution(tmp_path: Path, raw: str) -> None:
    stages: list[str] = []
    result = await call_file(ReadFileTool(tmp_path), {"path": raw}, stages=stages)
    assert result.error_type == "permission_denied"
    assert not result.outcome_unknown
    assert stages == []


# 功能：验证外部审批仅允许单次决策，历史授权不能跳过，非法响应不会消费等待请求
# 设计：连续读取两次真实外部归档，在事件中核验重连元数据并先发送非法 always 决策
async def test_external_approval_is_once_and_validated(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    archive = tmp_path / "outputs" / "full.txt"
    archive.parent.mkdir()
    archive.write_text("full archived output", encoding="utf-8")
    policy_file = tmp_path / "policy.toml"
    manager = PermissionManager(policy_file=policy_file)
    requests: list[PermissionRequestedEvent] = []
    bus = EventBus()

    # 模拟旧客户端误发永久授权，再使用有效单次决策完成同一请求
    async def approve(event: BaseModel) -> None:
        if not isinstance(event, PermissionRequestedEvent):
            return
        requests.append(event)
        assert event.workspace == str(root)
        assert event.resolved_path == str(archive)
        assert event.outside_workspace
        assert event.allowed_decisions == ["allow_once", "deny_once"]
        for invalid in ("always_allow", "always_deny", "made_up"):
            with pytest.raises(ValueError):
                manager.respond(event.request_id, invalid, session_id=event.session_id, run_id=event.run_id)
        pending = manager.pending_for_session("session-one")
        assert pending[0]["resolved_path"] == str(archive)
        assert pending[0]["allowed_decisions"] == event.allowed_decisions
        manager.respond(event.request_id, "allow_once", session_id=event.session_id, run_id=event.run_id)

    bus.subscribe(approve)
    for uid in ("one", "two"):
        result = await call_file(ReadFileTool(root), {"path": str(archive)}, manager, bus, uid)
        assert result.content.endswith("1 | full archived output")
    assert len(requests) == 2
    assert manager.pending_for_session("session-one") == []
    assert not policy_file.exists()


# 功能：验证无法解析的父路径和 Windows 驱动器相对路径不进入执行
# 设计：已有文件不能成为目录，驱动器相对路径不能借进程 cwd 改变含义
async def test_unresolvable_paths_are_denied(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("file", encoding="utf-8")
    paths = ["file.txt/child.txt"]
    if os.name == "nt":
        paths.append("C:relative.txt")
    for raw in paths:
        result = await call_file(WriteFileTool(tmp_path), {"path": raw, "content": "x"})
        assert result.error_type == "permission_denied"
        assert not result.outcome_unknown


# 功能：验证外部写入被拒绝或超时后不创建目录和文件，单次允许后正常写入
# 设计：同一真实目标依次测试拒绝、超时、批准，直接检查磁盘副作用和执行边界
async def test_external_write_denial_timeout_and_allow(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "new" / "nested" / "file.txt"
    manager = PermissionManager(timeout_s=0.02)
    bus = EventBus()

    # 对本次请求明确拒绝
    async def deny(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            manager.respond(event.request_id, "deny_once", session_id=event.session_id, run_id=event.run_id)

    bus.subscribe(deny)
    for active_bus in (bus, EventBus()):
        stages: list[str] = []
        result = await call_file(
            WriteFileTool(root), {"path": str(target), "content": "ok"},
            manager, active_bus, stages=stages,
        )
        assert result.error_type == "permission_denied"
        assert not result.outcome_unknown
        assert stages == ["awaiting_approval"]
        assert not target.parent.exists()

    # 对新的一次调用批准，不修改工具级策略
    async def allow(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            manager.respond(event.request_id, "allow_once", session_id=event.session_id, run_id=event.run_id)

    bus = EventBus()
    bus.subscribe(allow)
    result = await call_file(
        WriteFileTool(root), {"path": str(target), "content": "ok"}, manager, bus,
    )
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "ok"


# 功能：验证静态禁止优先于外部审批和旧 always_allow
# 设计：人为设置冲突策略，审批事件不得出现且磁盘保持不变
async def test_static_deny_cannot_be_overridden(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside.txt"
    manager = PermissionManager({"write_file": ToolPolicy(default=PermissionDecision.DENY)})
    result = await call_file(
        WriteFileTool(root), {"path": str(target), "content": "no"}, manager,
    )
    assert result.error_type == "permission_denied"
    assert not manager._pending
    assert not target.exists()


# 功能：验证链接和 junction 的目标决定边界，新文件解析已有链接父目录
# 设计：在真实链接目录下写入不存在的文件，确认预检查能发现工作区外实际位置
async def test_linked_parent_and_directory_recursion(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("hidden", encoding="utf-8")
    link = root / "linked"
    directory_link(link, outside)
    try:
        access = WorkspacePaths(root).inspect("linked/new/file.txt")
        assert access.target == outside / "new" / "file.txt"
        assert access.outside_workspace
        with pytest.raises(WorkspacePathError):
            await WriteFileTool(root).invoke({"path": "linked/new/file.txt", "content": "x"})
        assert not (outside / "new").exists()
        listing = await ListDirTool(root).invoke({"path": ".", "max_depth": 4})
        assert "linked [link]" in listing.content
        assert "secret.txt" not in listing.content
    finally:
        unlink_directory(link)


# 功能：验证审批期间替换链接会拒绝访问新目标，不重试也不标为未知
# 设计：审批事件回调中替换真实 junction，核验 executing 提交尚未发生
@pytest.mark.parametrize("internal", [False, True])
async def test_changed_target_after_approval_denied(tmp_path: Path, internal: bool) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first, second = ((root if internal else tmp_path) / name for name in ("first", "second"))
    for directory in (first, second):
        directory.mkdir()
    link = root / "link"
    directory_link(link, first)
    manager = PermissionManager()
    bus = EventBus()
    requests: list[BaseModel] = []

    # 将批准时的目标换成另一个外部目录
    async def swap_and_allow(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            unlink_directory(link)
            directory_link(link, second)
            manager.respond(
                event.request_id, "always_allow" if internal else "allow_once",
                session_id=event.session_id, run_id=event.run_id,
            )

    bus.subscribe(swap_and_allow)
    stages: list[str] = []
    try:
        result = await call_file(
            WriteFileTool(root), {"path": "link/a.txt", "content": "x"}, manager, bus,
            stages=stages,
        )
        assert result.error_type == "permission_denied"
        assert "changed" in result.content
        assert not result.outcome_unknown
        assert stages == ["awaiting_approval"]
        assert len(requests) == 1
        assert not (first / "a.txt").exists()
        assert not (second / "a.txt").exists()
    finally:
        unlink_directory(link)


# 功能：验证内部链接与原文件复用授权，链接改指其他文件后必须重新询问
# 设计：使用真实 junction 和真实文件写入，排除仅靠原始路径或父目录复用许可
async def test_file_grant_follows_resolved_target(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "alias"
    directory_link(link, first)
    manager, bus = PermissionManager(), EventBus()
    requests: list[PermissionRequestedEvent] = []
    async def approve(event: BaseModel) -> None:
        if isinstance(event, PermissionRequestedEvent):
            requests.append(event)
            manager.respond(
                event.request_id, "always_allow" if len(requests) == 1 else "deny_once",
                session_id=event.session_id, run_id=event.run_id,
            )
    bus.subscribe(approve)
    tool = WriteFileTool(tmp_path)
    try:
        assert not (await call_file(tool, {"path": "alias/a.txt", "content": "one"}, manager, bus)).is_error
        assert not (await call_file(tool, {"path": "first/a.txt", "content": "two"}, manager, bus)).is_error
        assert len(requests) == 1
        unlink_directory(link)
        directory_link(link, second)
        result = await call_file(tool, {"path": "alias/a.txt", "content": "no"}, manager, bus)
        assert result.error_type == "permission_denied"
        assert [event.resolved_path for event in requests] == [str(first / "a.txt"), str(second / "a.txt")]
        assert (first / "a.txt").read_text(encoding="utf-8") == "two"
        assert not (second / "a.txt").exists()
    finally:
        unlink_directory(link)


# 功能：验证指向内部的文件符号链接可读，指向外部的文件符号链接需审批
# 设计：实际创建文件链接；系统缺少权限时明确跳过，不能用 mock 声称通过
async def test_file_symlink_boundary(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside, outside = root / "inside.txt", tmp_path / "outside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "local").symlink_to(inside)
        (root / "external").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")
    assert (await ReadFileTool(root).invoke({"path": "local"})).content.endswith("1 | inside")
    with pytest.raises(WorkspacePathError):
        await ReadFileTool(root).invoke({"path": "external"})


# 功能：验证主 Agent、恢复组装及嵌套子 Agent 共用 Run 中冻结的工作区
# 设计：先冻结 Run 再改变 cwd，直接检查实际组装的每层工具，避免依赖付费模型
def test_runner_restore_and_nested_agents_share_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    root, other = tmp_path / "root", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    monkeypatch.chdir(root)
    runner = AgentRunner(KamaConfig(), provider=AsyncMock(), runs_dir=tmp_path / "runs")
    context = ExecutionContext(run=runner._new_run_state("goal", "run-one", None))
    monkeypatch.chdir(other)
    for resuming in (False, True):
        loop = runner._build_loop(
            context, EventBus(), tmp_path / "runs" / "run-one", None, resuming=resuming,
        )
        registry = loop._tools.registry
        for depth in range(3):
            for name in ("read_file", "write_file", "list_dir", "get_file_outline",
                         "search_symbol", "read_symbol"):
                tool = registry.get(name)
                assert isinstance(tool, FileTool)
                assert tool.paths.root == root
            bash = registry.get("bash")
            assert isinstance(bash, BashTool)
            assert bash.workspace == root
            spawn = registry.get("spawn_agent")
            if depth == 2:
                assert spawn is None
                break
            assert isinstance(spawn, SpawnAgentTool)
            assert spawn._workspace == root
            registry = spawn._build_child_registry(EventBus(), f"child-{depth}", None)
