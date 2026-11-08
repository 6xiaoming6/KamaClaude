from __future__ import annotations

import math
from dataclasses import dataclass, field

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.kama/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.kama/config.toml"
_DEFAULT_MAX_STEPS = 0  # 0 表示不限步数，时间与无进展保护仍独立生效。
_DEFAULT_MODEL = "deepseek-flash"
_DEFAULT_TRACE_FILE = "~/.kama/traces/daemon.jsonl"


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS
    max_run_seconds: int = 1800
    max_total_tokens: int = 0
    repeat_failure_limit: int = 5
    no_progress_limit: int = 5

    def __post_init__(self) -> None:
        from kama_claude.core.progress import validate_progress_limit

        if type(self.max_steps) is not int or self.max_steps < 0:
            raise ValueError("max_steps must be a nonnegative integer (0 means unlimited)")
        validate_progress_limit(self.no_progress_limit)


@dataclass
class SubagentConfig:
    max_active: int = 2
    max_total: int = 6

    # 独立构造配置时同样检查正整数，避免绕过 TOML 和环境变量校验。
    def __post_init__(self) -> None:
        for value in (self.max_active, self.max_total):
            if type(value) is not int or value <= 0:
                raise ValueError("subagent limits must be positive integers")


@dataclass
class LlmConfig:
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 300.0  # 审批超时秒数；0 表示不超时


@dataclass
class ExecutionConfig:
    forward_env: list[str] = field(default_factory=list)  # 额外透传的变量名
    env: dict[str, str] = field(default_factory=dict)  # 明确传给命令的变量


@dataclass
class CompactionConfig:
    auto_threshold: float = 0.8  # 请求前按有效窗口的 80% 触发，0 表示关闭自动摘要。
    tool_result_limit: int = 32_000  # 固定字符数，避免普通代码片段过早裁剪。
    tool_result_keep: int = 24_000  # 头尾保留的总字符数
    context_window_tokens: int = 262_144  # 日常编码使用 256K 有效窗口，不能超过模型实际能力。
    output_reserve_ratio: float = 0.0625  # 256K 窗口对应 16K 单次输出。
    safety_margin_ratio: float = 0.02
    keep_recent_turns: int = 3
    recent_history_ratio: float = 0.25  # 256K 下最多保留 64K 近期原文。
    summary_max_ratio: float = 0.0625  # 256K 下摘要输出硬上限 16K。
    summary_target_ratio: float = 0.03125  # 256K 下软目标最多 8K，小历史再按比例缩小。

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.context_window_tokens) is not int or self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
        for name in (
            "output_reserve_ratio", "safety_margin_ratio", "recent_history_ratio",
            "summary_max_ratio", "summary_target_ratio",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"{name} must be a finite ratio between 0 and 1")
        if (
            type(self.auto_threshold) not in (int, float)
            or not math.isfinite(self.auto_threshold) or not 0 <= self.auto_threshold < 1
        ):
            raise ValueError("auto_threshold must be 0 (disabled) or a ratio below 1")
        if self.summary_target_ratio > self.summary_max_ratio:
            raise ValueError("summary_target_ratio must not exceed summary_max_ratio")
        if sum((self.output_reserve_ratio, self.safety_margin_ratio,
                self.recent_history_ratio, self.summary_max_ratio)) >= 1:
            raise ValueError("context allocations must leave space for fixed prompts and tools")
        reserve = max(self.output_reserve_ratio, self.summary_max_ratio) + self.safety_margin_ratio
        if self.auto_threshold and self.auto_threshold + reserve >= 1:
            raise ValueError("auto_threshold must leave room for output and safety margin")
        if self.input_limit <= 0 or self.summary_input_limit <= 0:
            raise ValueError("context window is too small for output and safety margin")

    # 都是派生额度，不另存固定 Token 数；修改窗口后下次读取即重新计算。
    @property
    def output_reserve_tokens(self) -> int:
        return max(1, int(self.context_window_tokens * self.output_reserve_ratio))

    @property
    def safety_margin_tokens(self) -> int:
        return max(1, math.ceil(self.context_window_tokens * self.safety_margin_ratio))

    @property
    def recent_token_budget(self) -> int:
        return max(1, int(self.context_window_tokens * self.recent_history_ratio))

    @property
    def summary_max_tokens(self) -> int:
        return max(1, int(self.context_window_tokens * self.summary_max_ratio))

    def summary_target_for(self, old_tokens: int) -> int:
        # 窗口比例是上限，小段旧历史不应被扩写成大摘要。
        window_target = int(self.context_window_tokens * self.summary_target_ratio)
        return max(1, min(window_target, old_tokens // 4))

    @property
    def trigger_tokens(self) -> int:
        return int(self.context_window_tokens * self.auto_threshold)

    def input_limit_for(self, output_tokens: int) -> int:
        return self.context_window_tokens - output_tokens - self.safety_margin_tokens

    @property
    # 输入预算始终预留输出空间和安全余量
    def input_limit(self) -> int:
        return self.input_limit_for(self.output_reserve_tokens)

    @property
    def summary_input_limit(self) -> int:
        return self.input_limit_for(self.summary_max_tokens)


@dataclass
class MemoryConfig:
    global_auto_ratio: float = 0.002  # 256K 下约 512 Token，不因窗口缩小而挤掉常驻信息。
    project_index_ratio: float = 0.004

    # 自动入口必须保持有限预算，用户规则不受这两项裁剪。
    def __post_init__(self) -> None:
        for value in (self.global_auto_ratio, self.project_index_ratio):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
                raise ValueError("memory ratios must be finite values between 0 and 1")


@dataclass
class ToolSearchConfig:
    enabled: bool = True
    db_path: str = "~/.kama/tool_catalog.sqlite3"
    model_path: str = ""
    top_k: int = 3
    max_loaded_schema_ratio: float = 0.04  # 256K 下约 10K，保留多 MCP 工具的加载空间。

    def schema_budget(self, window_tokens: int) -> int:
        return max(1, int(window_tokens * self.max_loaded_schema_ratio))

    # 空模型路径使用官方模型缓存；未安装可选推理库时搜索仍可降级。
    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("tool_search.enabled must be a boolean")
        if not isinstance(self.db_path, str) or not self.db_path.strip():
            raise ValueError("tool_search.db_path must be a nonempty string")
        if not isinstance(self.model_path, str):
            raise ValueError("tool_search.model_path must be a string")
        if type(self.top_k) is not int or not 1 <= self.top_k <= 5:
            raise ValueError("tool_search.top_k must be between 1 and 5")
        ratio = self.max_loaded_schema_ratio
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0 < ratio < 1:
            raise ValueError("tool_search.max_loaded_schema_ratio must be between 0 and 1")


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "streamable-http"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    forward_env: list[str] = field(default_factory=list)
    url: str = ""                 # Streamable HTTP 完整端点
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class KamaConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    subagent: SubagentConfig = field(default_factory=SubagentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tool_search: ToolSearchConfig = field(default_factory=ToolSearchConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
