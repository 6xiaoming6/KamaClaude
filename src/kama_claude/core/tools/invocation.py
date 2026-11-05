from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from kama_claude.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.base import FileTool, ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.errors import RateLimitedError, ToolError
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.workspace import WorkspacePathError

if TYPE_CHECKING:
    from kama_claude.core.permissions.manager import PermissionManager

type ToolCheckpointHook = Callable[[Literal["awaiting_approval", "executing"]], None]
_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0
_RETRYABLE = frozenset({"runtime_error", "rate_limited"})


# 返回当前 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 发布工具最终结果，持久化调用方应在保存成功后调用
async def publish_tool_result(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    result: ToolResult,
    elapsed_ms: int,
    *,
    attempt: int = 1,
) -> None:
    if result.is_error:
        await bus.publish_safe(
            ToolCallFailedEvent(
                run_id=run_id,
                tool_use_id=tool_call.id,
                tool_name=tool_call.name,
                error_class=result.error_type or "runtime_error",
                error_message=result.content,
                elapsed_ms=elapsed_ms,
                attempt=attempt,
                ts=_now(),
            )
        )
    else:
        await bus.publish_safe(
            ToolCallFinishedEvent(
                run_id=run_id,
                tool_use_id=tool_call.id,
                tool_name=tool_call.name,
                elapsed_ms=elapsed_ms,
                output=result.content,
                ts=_now(),
            )
        )


# 校验参数、检查权限并执行工具，返回结果；保存错误和取消直接向上传播
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
    workspace: Path | None = None,
    checkpoint_hook: ToolCheckpointHook | None = None,
    notify_result: bool = True,
) -> ToolResult:
    t0 = time.monotonic()
    await bus.publish_safe(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    # 普通调用直接通知；Loop 会延后到结果保存成功后再通知
    async def finish(result: ToolResult, attempt: int = 1) -> ToolResult:
        if notify_result:
            await publish_tool_result(
                bus, run_id, tool_call, result, int((time.monotonic() - t0) * 1000),
                attempt=attempt,
            )
        return result

    tool = registry.get(tool_call.name)
    if tool is None:
        return await finish(
            ToolResult(
                content=f"unknown tool: {tool_call.name}",
                is_error=True,
                error_type="runtime_error",
            )
        )
    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await finish(
                ToolResult(
                    content=str(exc),
                    is_error=True,
                    error_type="schema_error",
                )
            )
    params = dict(tool_call.input)
    # 路径预检查在审批和执行意图之前完成，拒绝不代表未知副作用
    try:
        path_access = (
            tool.path_access(params) if isinstance(tool, FileTool) else None
        )
    except WorkspacePathError as exc:
        return await finish(ToolResult(
            content=str(exc), is_error=True, error_type="permission_denied",
        ))
    if path_access is not None and path_access.outside_workspace and permission_manager is None:
        return await finish(ToolResult(
            content=f"outside workspace; approval unavailable: {path_access.target}",
            is_error=True, error_type="permission_denied",
        ))
    if permission_manager is not None:
        if checkpoint_hook:
            checkpoint_hook("awaiting_approval")

        # 将审批事件安全交给观察者，Future 的生命周期仍由权限管理器负责
        request_id = ""

        async def emit_permission(raw: dict[str, Any]) -> None:
            nonlocal request_id
            request_id = raw["request_id"]
            await bus.publish_safe(PermissionRequestedEvent(**raw))

        path_options = {"path_access": path_access} if path_access is not None else {}
        allowed, decision = await permission_manager.check_and_wait(
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=params,
            session_id=session_id,
            event_emitter=emit_permission,
            run_id=run_id,
            # 命令以工具实际绑定的 cwd 为准，独立构造工具时也不能借用别的目录授权。
            workspace=tool.workspace if isinstance(tool, BashTool) else workspace,
            **path_options,
        )
        if not allowed:
            if request_id:
                await bus.publish_safe(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        request_id=request_id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            return await finish(
                ToolResult(
                    content="Permission denied. Do not execute this operation.",
                    is_error=True,
                    error_type="permission_denied",
                )
            )
        if decision != "auto_allow":
            await bus.publish_safe(
                PermissionGrantedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    request_id=request_id,
                    decision=decision,
                    ts=_now(),
                )
            )

    for attempt in range(1, _MAX_RETRIES + 2):
        if isinstance(tool, FileTool) and path_access is not None:
            try:
                tool.paths.resolve(path_access.requested_path, approved_path=path_access.target)
            except WorkspacePathError as exc:
                return await finish(ToolResult(
                    content=str(exc), is_error=True, error_type="permission_denied",
                ))
        # 执行意图提交成功后，才允许进入可能产生副作用的工具函数
        if checkpoint_hook:
            checkpoint_hook("executing")
        try:
            # 可信批准路径作为 Python 关键字传入，不读取模型提供的同名参数
            operation = (
                tool.invoke(params, approved_path=path_access.target)
                if isinstance(tool, FileTool) and path_access is not None
                else tool.invoke(params)
            )
            result = await asyncio.wait_for(
                operation,
                timeout=timeout,
            )
        except WorkspacePathError as exc:
            result = ToolResult(content=str(exc), is_error=True, error_type="permission_denied")
        except ToolError as exc:
            result = ToolResult(content=str(exc), is_error=True, error_type=exc.error_type)
        except PermissionError as exc:
            result = ToolResult(
                content=str(exc), is_error=True,
                error_type="permission_denied" if tool.retry_safe else "runtime_error",
                outcome_unknown=not tool.retry_safe,
            )
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, UnicodeError) as exc:
            # 读取失败是明确错误；修改类工具未经明确分类的异常仍按未知副作用处理
            known = tool.retry_safe
            result = ToolResult(
                content=str(exc), is_error=True,
                error_type=("not_found" if isinstance(exc, FileNotFoundError)
                            else "invalid_operation") if known else "runtime_error",
                outcome_unknown=not known,
            )
        except RateLimitedError as exc:
            result = ToolResult(content=str(exc), is_error=True, error_type="rate_limited")
        # 超时不代表操作未发生，非安全工具要保守记录结果未知
        except TimeoutError:
            result = ToolResult(
                content=f"tool timed out after {timeout}s",
                is_error=True,
                error_type="timeout",
                outcome_unknown=not tool.retry_safe,
            )
        except Exception as exc:
            result = ToolResult(
                content=str(exc),
                is_error=True,
                error_type="runtime_error",
                outcome_unknown=not tool.retry_safe,
            )

        retry = (
            result.is_error
            and tool.retry_safe
            and result.error_type in _RETRYABLE
            and attempt <= _MAX_RETRIES
        )
        # 成功、不可重试或次数耗尽时，返回本次调用的最终结果
        if not retry:
            return await finish(result, attempt)
        await bus.publish_safe(
            ToolCallFailedEvent(
                run_id=run_id,
                tool_use_id=tool_call.id,
                tool_name=tool_call.name,
                error_class=result.error_type or "runtime_error",
                error_message=result.content,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                attempt=attempt,
                retrying=True,
                ts=_now(),
            )
        )
        await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")
