from __future__ import annotations

from typing import Any

from kama_claude.core.transport.socket_client import IpcError, SocketClient

DECISIONS = {"y": "allow_once", "a": "always_allow", "n": "deny_once", "d": "always_deny"}


class PermissionPrompts:
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, Any]] = {}

    def handle(self, event: dict[str, Any]) -> None:
        if event.get("type") == "permission.requested":
            self.pending[str(event["request_id"])] = event
            self.show()
        elif event.get("type") in {"permission.denied", "permission.granted"}:
            self.resolve(str(event.get("request_id", "")))

    def resolve(self, request_id: str) -> None:
        if self.pending.pop(request_id, None) is not None:
            self.show()

    def show(self) -> None:
        if not self.pending:
            return
        event = next(iter(self.pending.values()))
        name = str(event.get("tool_name", ""))
        print(f"[permission {event['request_id']}] {name}  {event.get('param_preview', '')}")
        if name == "bash":
            print(f"  command: {(event.get('params') or {}).get('command', '')}")
            print(f"  cwd: {event.get('workspace', '')} (local shell, not sandboxed)")
        if event.get("resolved_path"):
            print(f"  requested: {(event.get('params') or {}).get('path', '.')}")
            print(f"  target: {event['resolved_path']}")
        print(f"  reason: {event.get('reason', '')}")
        print(f"  scope: {event.get('approval_scope', '仅本次操作')}")
        labels = {
            "allow_once": "允许本次", "deny_once": "拒绝本次",
            "always_allow": (f"本会话允许使用 {name} 操作此文件"
                             if name in {"write_file", "edit_file"} else "本会话允许相同操作"),
            "always_deny": "本会话拒绝相同操作",
        }
        allowed = event.get("allowed_decisions", [])
        print("  " + "  ".join(f"{key}={labels[value]}" for key, value in DECISIONS.items()
                               if value in allowed))

    # 绑定读入时的请求，过期回答不能误用到下一项审批。
    async def answer(self, client: SocketClient, request_id: str, content: str) -> None:
        request = self.pending.get(request_id)
        if request is None:
            print("  approval expired; this answer was not applied to another request")
            return
        decision = DECISIONS.get(content.strip().lower())
        if decision not in request.get("allowed_decisions", []):
            print("  choose one of the decisions shown for this request")
            return
        try:
            await client.send_command("permission.respond", {
                "request_id": request_id, "decision": decision,
                "session_id": request["session_id"], "run_id": request["run_id"],
            })
        except IpcError as exc:
            print(f"  approval not submitted: {exc}")
            return
        self.resolve(request_id)
