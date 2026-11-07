from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from kama_claude.core.permissions.mode import PermissionMode
from kama_claude.core.session.model import SessionMode, SessionStatus


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]          # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"      # "global" | "run:<run_id>" | "session:<session_id>"
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str
    cursor: int = Field(default=0, ge=0, description="Byte offset returned by next_cursor.")
    history_end: int | None = Field(default=None, ge=0, description="Fixed snapshot end offset.")
    include_metadata: bool = False


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]
    next_cursor: int | None = None
    history_end: int = 0


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    request_id: str
    session_id: str
    run_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: Literal["allow_once", "always_allow", "deny_once", "always_deny"]


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int
    compacted: bool = True
    original_tokens: int = 0
    remaining_tokens: int = 0
    reason: str = ""
    applied_stages: list[str] = Field(default_factory=list)
    llm_used: bool = False
    preserved_messages: int = 0  # 摘要外保留的近期消息及必要发现记录数。


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"


class SessionListResult(BaseModel):
    sessions: list[dict[str, Any]]


class SessionOpenCommand(BaseModel):
    type: Literal["session.open"] = "session.open"
    session_id: str


class SessionOpenResult(BaseModel):
    session: dict[str, Any]
    revision: int
    messages: list[dict[str, Any]] = Field(
        description=(
            "First page of original conversation from thread.jsonl, not compacted model context. "
            "Follow next_cursor with session.get_history using the same history_end. "
            "Rows may include id and run_id for display correlation; these are not model input."
        ),
    )
    run: dict[str, Any] | None
    running: bool
    pending_permissions: list[dict[str, Any]]
    event_seq: int = 0
    next_cursor: int | None = None
    history_end: int = 0
    permission_mode: PermissionMode = PermissionMode.ASK


class SessionSetPermissionModeCommand(BaseModel):
    type: Literal["session.set_permission_mode"] = "session.set_permission_mode"
    session_id: str
    mode: PermissionMode


class SessionSetPermissionModeResult(BaseModel):
    permission_mode: PermissionMode


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str
    expected_revision: int
    action: Literal["continue", "retry_tool", "abort"] = "continue"


class SessionResumeResult(BaseModel):
    run_id: str
    status: str
    revision: int


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetHistoryCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand
    | SessionListCommand
    | SessionOpenCommand
    | SessionResumeCommand
    | SessionSetPermissionModeCommand,
    Discriminator("type"),
]
