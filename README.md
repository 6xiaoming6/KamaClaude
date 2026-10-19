# KamaClaude

本地 AI Agent 项目。`dev` 用于逐步整合各阶段的代码修复与个人改进，完成验证后再合并到 `main`。

当前已整合 S0 与 S1：保留 TCP 上的 JSON-RPC 通信、NDJSON 分帧、配置日志与 Windows 启停适配，并加入模型调用、读文件工具、Agent 执行循环和事件记录。模型适配包含 Anthropic、OpenAI 和 DeepSeek，当前默认使用 DeepSeek。TUI 等能力待后续阶段整合。

## 环境要求

- Python 3.12
- uv
- Windows、macOS 或 Linux；Core 保留 S0 的 Windows 启停适配，S1 任务在 CLI 进程内执行

## 运行

安装依赖：

```text
uv sync
```

在第一个终端中启动 Core，按 Ctrl+C 退出：

```text
uv run kama-core
```

在另一个终端中验证通信：

```text
uv run kama ping
uv run kama --version
```

默认监听 `127.0.0.1:7437`。`ping` 和版本查询无需模型 API key；配置方式见 [RUNBOOK.md](./RUNBOOK.md)。

## 模型适配与 Agent 任务

| 平台 | Provider | 接口方式 | API key | 模型名示例 |
| --- | --- | --- | --- | --- |
| Anthropic | `AnthropicProvider` | Anthropic Messages API | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| OpenAI | `OpenaiProvider` | Chat Completions，类内转换消息与工具格式 | `OPENAI_API_KEY` | `gpt-4.1-mini` |
| DeepSeek（当前默认） | `DeepseekProvider` | Anthropic 兼容接口，复用 Anthropic Provider 的 `chat()` | `DEEPSEEK_API_KEY` | `deepseek-flash` |

三种实现都在 [provider.py](./src/kama_claude/core/llm/provider.py)，返回统一的 `LlmResponse`。DeepSeek 使用 `https://api.deepseek.com/anthropic`；其兼容接口会忽略 `cache_control` 与 `is_error`，工具错误信息仍需保存在结果内容中。[DeepSeek 官方兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)

在项目根目录的 `.env` 中设置：

```dotenv
DEEPSEEK_API_KEY=替换为你的密钥
KAMA_LLM_DEFAULT_MODEL=deepseek-flash
KAMA_MAX_STEPS=20
```

`.env.example` 和代码内的模型默认值仍保留 Anthropic 示例，使用当前 DeepSeek 入口时应显式覆盖模型名；已有系统环境变量优先于 `.env`。

然后直接运行，无需启动 Core：

```text
uv run kama run --goal "请使用 read_file 读取 README.md，并介绍这个项目"
```

任务事件写入当前工作目录下的 `runs/<run_id>/events.jsonl`。一次任务包含多轮模型与工具交互，默认最多 20 轮；模型返回 `end_turn` 时将任务标记为成功。

当前默认 Provider 固定为 DeepSeek，没有 `KAMA_LLM_PROVIDER` 配置开关。其他平台通过 Runner 的 `provider` 参数显式注入，例如下面的 OpenAI 入口；使用前需额外安装 `openai` SDK 并设置 `OPENAI_API_KEY`，当前依赖声明尚未自动安装该 SDK。

```python
import asyncio

from kama_claude.core.config import get_config
from kama_claude.core.llm.provider import OpenaiProvider
from kama_claude.core.runner import AgentRunner

runner = AgentRunner(get_config(), provider=OpenaiProvider(model="gpt-4.1-mini"))
asyncio.run(runner.run("请使用 read_file 读取 README.md，并介绍这个项目"))
```

将示例中的 Provider 换为 `AnthropicProvider` 或 `DeepseekProvider`，同时使用对应的密钥和模型名，即可复用同一任务入口。模型调用会产生服务端用量。

## 开发验证

```text
uv run python -m pytest tests/ -m "not integration" -v
uv run ruff check src tests scripts
uv run mypy src
uv run python -X utf8 scripts/gen_protocol_doc.py --check
```

上述 pytest 命令排除真实模型测试。历史模型端到端用例仍检查 Anthropic 密钥，而 Runner 默认已是 DeepSeek，其门禁与模型配置尚待同步。协议文档同步检查也仍有既有的排版和字段标记差异。

## 阶段整合约定

各阶段分支保留各自的学习用 README 和 `docs/learning/`。`dev` 独立维护本 README，不跟踪阶段学习文档；协议与运行文档继续随代码维护。

后续合并阶段代码时，应在提交合并结果前检查差异，保留 `dev` 的 README，并排除新增或修改的学习文档。`.gitignore` 只避免未跟踪的学习文件被误加入，不能阻止其他分支已跟踪的文件通过合并进入 `dev`，因此每次合并都需要检查。

学习文档仍保存在阶段分支的历史中。由于 `dev` 从已有 S0 提交创建，早期提交也包含这些文档；本分支当前版本不再跟踪它们，不重写已有历史。

## 项目文档

- [RUNBOOK.md](./RUNBOOK.md)：配置、开发命令与故障排查。
- [WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)：由代码生成的 IPC 协议说明。
