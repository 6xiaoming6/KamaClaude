from __future__ import annotations

from dataclasses import dataclass, field

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.kama/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.kama/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRACE_FILE = "~/.kama/traces/daemon.jsonl"


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS
    max_run_seconds: int = 0
    max_total_tokens: int = 0
    repeat_failure_limit: int = 5


@dataclass
class SubagentConfig:
    max_active: int = 3
    max_total: int = 8

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
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时


@dataclass
class ExecutionConfig:
    forward_env: list[str] = field(default_factory=list)  # 额外透传的变量名
    env: dict[str, str] = field(default_factory=dict)  # 明确传给命令的变量


@dataclass
class CompactionConfig:
    auto_threshold: float = 0.8  # 请求前按估算输入触发，0 表示关闭自动摘要
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000  # 头尾保留的总字符数
    context_window_tokens: int = 65_536  # 按实际模型配置，并非模型能力声明
    output_reserve_tokens: int = 8_192
    keep_recent_turns: int = 3
    recent_token_budget: int = 16_384
    summary_max_tokens: int = 2_048

    @property
    # 输入预算始终预留输出空间和安全余量
    def input_limit(self) -> int:
        return self.context_window_tokens - self.output_reserve_tokens - 1_024


@dataclass
class MemoryConfig:
    global_auto_tokens: int = 512
    project_index_tokens: int = 1_024

    # 自动入口必须保持有限预算，用户规则不受这两项裁剪。
    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.global_auto_tokens, self.project_index_tokens,
        )):
            raise ValueError("memory budgets must be positive integers")


@dataclass
class ToolSearchConfig:
    enabled: bool = True
    db_path: str = "~/.kama/tool_catalog.sqlite3"
    model_path: str = ""
    top_k: int = 3
    max_loaded_schema_tokens: int = 8_192

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
        if type(self.max_loaded_schema_tokens) is not int or self.max_loaded_schema_tokens <= 0:
            raise ValueError("tool_search.max_loaded_schema_tokens must be positive")


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
