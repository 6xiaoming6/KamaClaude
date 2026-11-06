from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from kama_claude.core.memory.model import MemorySnapshot
from kama_claude.core.skills.model import SkillEntry, SkillSnapshot


class CheckpointError(RuntimeError):
    pass


# 最近一次有效 Usage 的输入、输出及其覆盖前缀，不保存另一份对话内容。
class UsageAnchor(BaseModel):
    input_tokens: int = Field(gt=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    message_count: int = Field(ge=0, strict=True)
    prefix_hash: str

# 某一个工具的检查点
# 记录工具状态：等待执行、等待审批、执行中、结果未知、已完成
# 恢复时根据状态判断是否需要重新执行
class ToolCheckpoint(BaseModel):
    id: str
    name: str
    input: dict[str, Any]
    status: Literal["pending", "awaiting_approval", "executing", "unknown", "completed"] = "pending"
    result: dict[str, Any] | None = None
    attempts: int = 0
    output_prepared: bool = True  # 旧快照已在历史中；新调用显式设为 False。
    exposed: bool = True  # 回复产生时是否展示过此工具，防止同批先搜索再越过加载步骤。

# 一次 Run 的检查点：目标、当前步骤和状态
# 保存恢复阶段，以及当前工具批次应该从哪里继续
class RunCheckpoint(BaseModel):
    run_id: str
    goal: str
    model: str
    provider: str = "deepseek"
    workspace: str
    system_prompt: str = ""
    # 单独保存基础提示；本轮 system 冻结，摘要只刷新自动记忆快照。
    base_system_prompt: str = ""
    prompt_layout: Literal["legacy", "stable_v1"] = "legacy"
    memory_project_root: str = ""
    # 仅记录本轮真正注入的内容，长期记忆仍以 Markdown 为准
    memory_snapshot: MemorySnapshot | None = None
    skill_catalog: list[SkillEntry] | None = None  # None 是旧 Run，不在恢复中插入新工具。
    active_skill: SkillSnapshot | None = None
    tool_whitelist: list[str] | None = None
    tool_schemas: list[dict[str, Any]] = Field(default_factory=list)
    schemas_frozen: bool = False
    user_input: str = ""  # 真实用户原文，不从工具结果或补充上下文猜测。
    tool_loading_mode: Literal["all", "on_demand", "deferred"] = "all"
    mcp_tool_names: list[str] = Field(default_factory=list)
    mcp_definition_hashes: dict[str, str] = Field(default_factory=dict)
    loaded_tool_names: list[str] = Field(default_factory=list)
    max_loaded_schema_tokens: int = Field(default=8_192, gt=0)
    max_steps: int
    max_run_seconds: int = Field(default=0, ge=0)
    max_total_tokens: int = Field(default=0, ge=0)
    repeat_failure_limit: int = Field(default=5, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    actual_tokens: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    usage_complete: bool = True
    failure_fingerprint: str = ""
    repeated_failures: int = Field(default=0, ge=0)
    step: int = 0
    status: Literal["running", "interrupted", "success", "failed"] = "running"
    phase: Literal["ready", "llm", "tools", "finished"] = "ready"
    tools: list[ToolCheckpoint] = Field(default_factory=list)
    tool_index: int = 0
    reason: str | None = None
    result: str = ""
    context_pct: float = 0.0
    subagents: dict[str, str] = Field(default_factory=dict)
    decisions: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    # 恢复时拒绝损坏的可见列表，不能静默删掉或加载冻结目录外工具。
    def validate_loaded_tools(self) -> Self:
        if self.schemas_frozen and self.tool_loading_mode in ("on_demand", "deferred"):
            names = {str(schema["name"]) for schema in self.tool_schemas}
            if len(set(self.loaded_tool_names)) != len(self.loaded_tool_names):
                raise ValueError("duplicate loaded tool names")
            if not set(self.loaded_tool_names) <= set(self.mcp_tool_names) <= names:
                raise ValueError("loaded MCP tool is outside frozen catalog")
        return self

# 一个完整的检查点(当前 session 信息 + 模型工作 messages + 当前 Run)
class Checkpoint(BaseModel):
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    updated_at: str = ""
    session: dict[str, Any]
    messages: list[dict[str, Any]] = Field(default_factory=list)
    run: RunCheckpoint | None = None
    # 原始消息先随快照提交，再幂等追加到 thread；不是另一份模型工作历史
    history_pending: list[dict[str, Any]] = Field(default_factory=list)
    history_initialized: bool = False
    # 最近输入、输出 Usage 与覆盖前缀指纹；不保存 tokenizer 或滑动样本。
    token_estimation: dict[str, UsageAnchor] = Field(default_factory=dict)

    @model_validator(mode="after")
    # 校验消息形状，防止合法 JSON 中的损坏内容进入界面或模型请求
    def validate_messages(self) -> Self:
        for message in [*self.messages, *self.history_pending]:
            if message.get("role") not in ("user", "assistant"):
                raise ValueError("invalid message role")
            content = message.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or any(
                not isinstance(block, dict) or not isinstance(block.get("type"), str)
                for block in content
            ):
                raise ValueError("invalid message content")
        return self


# 以单会话原子快照保存权威状态，并用修订号拒绝旧快照覆盖
class CheckpointStore:
    # 保存根目录，不在初始化时创建或修改会话
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    # 将会话标识限制在根目录下，避免恢复入口访问任意文件
    # 例如~/.kama/sessions/sess-xxx/checkpoint.json
    def path(self, sid: str) -> Path:
        if not sid or Path(sid).name != sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            raise CheckpointError("invalid session id")
        return self.root / sid / "checkpoint.json"

    # 检查一下当前session下有没有已经存在的检查点
    def exists(self, sid: str) -> bool:
        return self.path(sid).exists()

    # 读取检查点并校验数据结构，不改变执行状态
    def load(self, sid: str) -> Checkpoint:
        try:
            # 1.读取 JSON
            raw = json.loads(self.path(sid).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema_version") != 1:
                raise CheckpointError("unsupported or missing checkpoint version")
            # 2.格式检验并转成 Pydantic 模型
            cp = Checkpoint.model_validate(raw)
            # 3.做一些必要性检验
            if cp.session.get("id") != sid:
                raise CheckpointError("checkpoint session mismatch")
            if cp.run and not 0 <= cp.run.tool_index <= len(cp.run.tools):
                raise CheckpointError("invalid tool cursor")
            if cp.run and cp.run.phase == "tools":
                run = cp.run
                ids = [t.id for t in run.tools]
                if len(ids) != len(set(ids)):
                    raise CheckpointError("duplicate tool identifiers")
                for tool in run.tools[: run.tool_index]:
                    if tool.status != "completed" or tool.result is None:
                        raise CheckpointError("tool cursor skips an uncommitted result")
                if any(t.status == "completed" for t in run.tools[run.tool_index :]):
                    raise CheckpointError("tool result and cursor are inconsistent")
            return cp
        except (OSError, ValueError, ValidationError) as exc:
            raise CheckpointError(f"cannot load checkpoint for {sid}: {exc}") from exc

    # 先同步临时文件再原子替换，只有替换成功后才推进内存修订号
    def save(self, cp: Checkpoint) -> None:
        sid = str(cp.session["id"])
        path = self.path(sid)
        temp_path: Path | None = None
        try:
            # 1.先检查要保存的检查点是否是最新的版本，不能拿旧版本覆盖新版本
            if path.exists() and self.load(sid).revision != cp.revision:
                raise CheckpointError("checkpoint revision conflict")

            # 2.先深拷贝要保存的检查点，创建临时文件保存
            candidate = cp.model_copy(deep=True)
            candidate = Checkpoint.model_validate(candidate.model_dump(mode="json"))
            candidate.revision += 1
            candidate.updated_at = datetime.now(UTC).isoformat()
            path.parent.mkdir(parents=True, exist_ok=True)
                # 把候选检查点先写入临时文件
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".checkpoint-",
                suffix=".tmp",
                delete=False,
            ) as f:
                temp_path = Path(f.name)
                json.dump(candidate.model_dump(mode="json"), f, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            # 3.临时检查点文件写入完成后，再替换更新旧的检查点
            os.replace(temp_path, path)
            # 4.同步更新内存里面的检查点版本号和更新时间
            cp.revision = candidate.revision
            cp.updated_at = candidate.updated_at
        except (OSError, TypeError, ValueError) as exc:
            raise CheckpointError(f"cannot commit checkpoint for {sid}: {exc}") from exc
        finally:
            # 5.最后清理临时文件
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
