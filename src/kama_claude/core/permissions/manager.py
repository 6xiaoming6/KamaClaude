from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from kama_claude.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    assess,
    param_preview,
)
from kama_claude.core.permissions.scope import ApprovalScope, approval_scope
from kama_claude.core.permissions.storage import load_policy_file
from kama_claude.core.workspace import PathAccess

logger = logging.getLogger(__name__)
_DEFAULT_DECISIONS = ("allow_once", "always_allow", "deny_once", "always_deny")
_ONCE_DECISIONS = ("allow_once", "deny_once")


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    payload: dict[str, Any]


class PermissionManager:
    def __init__(
        self, policies: dict[str, ToolPolicy] | None = None, *,
        policy_file: Path | None = None, timeout_s: float = 60.0,
    ) -> None:
        self._policies = dict(DEFAULT_POLICIES if policies is None else policies)
        self._pending: dict[str, _PendingRequest] = {}
        self._session_grants: dict[ApprovalScope, bool] = {}
        # 旧文件只保留拒绝记录；旧工具级允许不再自动放行，也不改写原文件。
        stored = load_policy_file(policy_file) if policy_file is not None else {}
        self._persistent_denials = {name for name, value in stored.items() if value == "deny"}
        if "allow" in stored.values():
            logger.warning("legacy tool-wide allows ignored; approve scoped operations again")
        self._timeout_s = timeout_s

    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        if self._is_persistently_denied(tool_name, params):
            return PermissionDecision.DENY
        return assess(tool_name, params, self._policies.get(tool_name)).decision

    def _is_persistently_denied(self, tool_name: str, params: dict[str, Any]) -> bool:
        return tool_name in self._persistent_denials or (
            tool_name == "memory_write" and params.get("scope", "project") == "global"
            and "memory_write_global" in self._persistent_denials
        )

    # 先统一判断禁止和强制审批，再考虑本会话内的有限授权。
    async def check_and_wait(
        self, tool_use_id: str, tool_name: str, params: dict[str, Any], session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        path_access: PathAccess | None = None, *, run_id: str = "",
        workspace: Path | None = None,
    ) -> tuple[bool, str]:
        params = deepcopy(params)
        assessment = assess(
            tool_name, params, self._policies.get(tool_name), path_access=path_access,
        )
        if (
            assessment.decision == PermissionDecision.DENY
            or self._is_persistently_denied(tool_name, params)
        ):
            return False, "auto_deny"
        root = path_access.workspace if path_access is not None else (workspace or Path.cwd())
        scope, description = approval_scope(session_id, root, tool_name, params, path_access)
        reusable = assessment.reusable and bool(session_id)
        if reusable and scope in self._session_grants:
            allowed = self._session_grants[scope]
            return allowed, "auto_allow" if allowed else "auto_deny"
        if assessment.decision == PermissionDecision.ALLOW:
            return True, "auto_allow"

        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        payload: dict[str, Any] = {
            "type": "permission.requested", "request_id": request_id,
            "run_id": run_id, "session_id": session_id,
            "tool_use_id": tool_use_id, "tool_name": tool_name,
            "params": params, "param_preview": param_preview(tool_name, params),
            "workspace": str(root.resolve()), "reason": assessment.reason,
            "approval_scope": description if reusable else "仅本次操作",
            "allowed_decisions": list(_DEFAULT_DECISIONS if reusable else _ONCE_DECISIONS),
            "ts": datetime.now(UTC).isoformat(),
        }
        if path_access is not None:
            payload.update(
                resolved_path=str(path_access.target),
                outside_workspace=path_access.outside_workspace,
            )
        self._pending[request_id] = _PendingRequest(future, payload)
        try:
            # 观察者和重连返回值拿到副本，不能修改绑定本次审批的参数与身份。
            await event_emitter(deepcopy(payload))
            raw = (
                await asyncio.wait_for(future, self._timeout_s)
                if self._timeout_s > 0 else await future
            )
            if request_id not in self._pending:
                return False, "deny_once"  # 会话已关闭，即使回答先到达也不能重新授予权限。
            # 等待期间策略可能被收紧，已有回答不能覆盖新的硬性禁止。
            if self.evaluate(tool_name, params) == PermissionDecision.DENY:
                return False, "auto_deny"
            allowed = raw in {"allow_once", "always_allow"}
            if reusable and raw in {"always_allow", "always_deny"}:
                self._session_grants[scope] = allowed
            return allowed, raw
        except TimeoutError:
            return False, "timeout"
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    # 只重现活跃请求，不持久化 Future 和单次授权。
    def pending_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return [
            deepcopy(req.payload) for req in self._pending.values()
            if req.payload["session_id"] == session_id and not req.future.done()
        ]

    def respond(
        self, request_id: str, decision: str, *, session_id: str, run_id: str,
    ) -> None:
        req = self._pending.get(request_id)
        if req is None or req.future.done():
            raise ValueError("approval request has expired or was already answered")
        if (session_id, run_id) != (req.payload["session_id"], req.payload["run_id"]):
            raise ValueError("approval does not belong to this session/run")
        if decision not in req.payload["allowed_decisions"]:
            raise ValueError(f"decision is not allowed for this request: {decision}")
        req.future.set_result(decision)

    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        for request_id, req in list(self._pending.items()):
            if req.payload["session_id"] == session_id:
                self._pending.pop(request_id)
                if not req.future.done():
                    req.future.set_result("deny_once")
        self._session_grants = {
            key: value for key, value in self._session_grants.items()
            if key.session_id != session_id
        }
        logger.debug("permission: cleared session=%s reason=%s", session_id, reason)
