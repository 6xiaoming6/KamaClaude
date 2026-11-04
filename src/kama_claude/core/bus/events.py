from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field


# 描述 Core 启动后的监听地址和版本信息
class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    listen_addr: str  # e.g. "127.0.0.1:7437"
    version: str


# 新一轮任务开始，携带 Run ID 和用户目标
class RunStartedEvent(BaseModel):
    type: Literal["run.started"] = "run.started"
    run_id: str
    goal: str
    ts: str  # ISO 8601


# 本轮任务结束，记录成功或失败状态、原因及总步数
class RunFinishedEvent(BaseModel):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str  # "success" | "failed"
    reason: str | None = None  # "exceeded_max_steps" | "cancelled" | "llm_error" | ...
    steps: int
    ts: str


# 任务执行中断或检查点提交失败，通知原因和当前修订号
class RunInterruptedEvent(BaseModel):
    type: Literal["run.interrupted"] = "run.interrupted"
    run_id: str
    session_id: str
    reason: str
    revision: int
    ts: str


# 原 Run 开始续跑，保留原任务身份和 Step
class RunResumedEvent(BaseModel):
    type: Literal["run.resumed"] = "run.resumed"
    run_id: str
    session_id: str
    step: int
    ts: str


# 当前逻辑步骤开始请求模型，恢复同一步时也可能再次发出
class StepStartedEvent(BaseModel):
    type: Literal["step.started"] = "step.started"
    run_id: str
    step: int
    ts: str


# 当前逻辑步骤处理结束，整轮任务仍可能继续下一步
class StepFinishedEvent(BaseModel):
    type: Literal["step.finished"] = "step.finished"
    run_id: str
    step: int
    ts: str


# 开始处理一次工具调用，携带工具名、参数和调用 ID
class ToolCallStartedEvent(BaseModel):
    type: Literal["tool.call_started"] = "tool.call_started"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    ts: str


# 工具成功返回，提供输出内容和执行耗时
class ToolCallFinishedEvent(BaseModel):
    type: Literal["tool.call_finished"] = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    elapsed_ms: int
    output: str = ""  # tool result content, for TUI display
    ts: str


# 工具处理出错，retrying 表示是否还会自动重试
class ToolCallFailedEvent(BaseModel):
    type: Literal["tool.call_failed"] = "tool.call_failed"
    run_id: str
    tool_use_id: str
    tool_name: str
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "rate_limited"
    error_class: str
    error_message: str
    elapsed_ms: int
    retrying: bool = False
    attempt: int = 1  # 1=first attempt, 2=first retry, 3=second retry
    ts: str


# 模型生成的流式文本片段，供前端逐段显示
class LlmTokenEvent(BaseModel):
    type: Literal["llm.token"] = "llm.token"
    run_id: str
    token: str
    ts: str


# 一次模型回复的 Token 用量、缓存统计及上下文占用比例
class LlmUsageEvent(BaseModel):
    type: Literal["llm.usage"] = "llm.usage"
    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    context_pct: float = 0.0
    ts: str


# 记录本次调用选择的模型和选择策略
class LlmModelSelectedEvent(BaseModel):
    type: Literal["llm.model_selected"] = "llm.model_selected"
    run_id: str
    model: str
    strategy: str  # "static" | "rule_based" | "cost_budget"
    ts: str


# 面向客户端的运行日志，包含级别、来源和消息内容
class LogLineEvent(BaseModel):
    type: Literal["log.line"] = "log.line"
    run_id: str
    level: str  # "DEBUG" | "INFO" | "WARNING" | "ERROR"
    source: str
    message: str
    ts: str


# 新会话已创建，通知会话 ID 和交互模式
class SessionCreatedEvent(BaseModel):
    type: Literal["session.created"] = "session.created"
    session_id: str
    mode: str
    ts: str


# Core 已接收并登记一条会话输入
class SessionMessageReceivedEvent(BaseModel):
    type: Literal["session.message_received"] = "session.message_received"
    session_id: str
    content: str
    ts: str


# 聊天会话本轮执行结束，等待用户发送下一条消息
class SessionWaitingForInputEvent(BaseModel):
    type: Literal["session.waiting_for_input"] = "session.waiting_for_input"
    session_id: str
    last_run_id: str
    ts: str


# 空闲会话收到新消息，继续交互；不同于中断 Run 的续跑
class SessionResumedEvent(BaseModel):
    type: Literal["session.resumed"] = "session.resumed"
    session_id: str
    ts: str


# 会话已关闭，保留历史但不能继续发送消息
class SessionClosedEvent(BaseModel):
    type: Literal["session.closed"] = "session.closed"
    session_id: str
    ts: str


# 正在生成压缩候选，尚未替换或提交历史
class ContextCompactionStartedEvent(BaseModel):
    type: Literal["context.compaction_started"] = "context.compaction_started"
    session_id: str
    run_id: str
    original_tokens: int
    ts: str


# 分层处理未全部完成，说明原因及已提交的程序层
class ContextCompactionFailedEvent(BaseModel):
    type: Literal["context.compaction_failed"] = "context.compaction_failed"
    session_id: str
    run_id: str
    reason: str
    applied_stages: list[str] = Field(default_factory=list)
    llm_used: bool = False
    ts: str


# 上下文压缩已提交，所有 Token 数为估算值
class ContextCompactedEvent(BaseModel):
    type: Literal["context.compacted"] = "context.compacted"
    session_id: str
    run_id: str
    original_tokens: int
    summary_tokens: int
    remaining_tokens: int = 0
    applied_stages: list[str] = Field(default_factory=list)
    llm_used: bool = False
    ts: str


# 工具调用需要审批，展示参数并等待用户决定(通知前端)
class PermissionRequestedEvent(BaseModel):
    type: Literal["permission.requested"] = "permission.requested"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    param_preview: str
    session_id: str
    ts: str


# 工具调用获准，记录本次审批决定
class PermissionGrantedEvent(BaseModel):
    type: Literal["permission.granted"] = "permission.granted"
    run_id: str
    tool_use_id: str
    # "allow_once" | "always_allow" | "auto_allow"
    decision: str
    ts: str


# 工具调用被拒绝或审批超时，阻止本次执行
class PermissionDeniedEvent(BaseModel):
    type: Literal["permission.denied"] = "permission.denied"
    run_id: str
    tool_use_id: str
    # "deny_once" | "always_deny" | "auto_deny"
    decision: str
    ts: str


# 通知子 Agent 的任务描述及其与父 Run 的关联
class SubagentStartedEvent(BaseModel):
    type: Literal["subagent.started"] = "subagent.started"
    run_id: str          # 子 agent run_id
    parent_run_id: str
    description: str
    ts: str


# 子 Agent 执行结束，向父任务报告执行状态
class SubagentFinishedEvent(BaseModel):
    type: Literal["subagent.finished"] = "subagent.finished"
    run_id: str
    parent_run_id: str
    status: str          # "success" | "failed"
    ts: str


# 会话调用了指定 Skill，记录名称、参数和所属 Run
class SkillInvokedEvent(BaseModel):
    type: Literal["skill.invoked"] = "skill.invoked"
    skill_name: str
    arguments: str
    run_id: str
    ts: str


# 根据 type 字段决定事件类型的判别联合
Event = Annotated[
    CoreStartedEvent
    | RunStartedEvent
    | RunFinishedEvent
    | RunInterruptedEvent
    | RunResumedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | ToolCallFailedEvent
    | LlmTokenEvent
    | LlmUsageEvent
    | LlmModelSelectedEvent
    | LogLineEvent
    | SessionCreatedEvent
    | SessionMessageReceivedEvent
    | SessionWaitingForInputEvent
    | SessionResumedEvent
    | SessionClosedEvent
    | ContextCompactedEvent
    | ContextCompactionStartedEvent
    | ContextCompactionFailedEvent
    | PermissionRequestedEvent
    | PermissionGrantedEvent
    | PermissionDeniedEvent
    | SubagentStartedEvent
    | SubagentFinishedEvent
    | SkillInvokedEvent,
    Discriminator("type"),
]
