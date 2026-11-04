from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from kama_claude.core.memory.model import MemoryScope, MemorySnapshot
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


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str


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
        description="Full original conversation from thread.jsonl, not compacted model context.",
    )
    run: dict[str, Any] | None
    running: bool
    pending_permissions: list[dict[str, Any]]
    event_seq: int = 0


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str
    expected_revision: int
    action: Literal["continue", "retry_tool", "abort"] = "continue"


class SessionResumeResult(BaseModel):
    run_id: str
    status: str
    revision: int


class MemoryInspectCommand(BaseModel):
    type: Literal["memory.inspect"] = "memory.inspect"
    session_id: str
    scope: MemoryScope | None = None
    key: str | None = None
    query: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class MemoryInspectResult(BaseModel):
    workspace: str
    layers: list[dict[str, Any]]
    entry: dict[str, Any] | None
    run_id: str | None
    snapshot: MemorySnapshot | None


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
    | MemoryInspectCommand,
    Discriminator("type"),
]
