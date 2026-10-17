# KamaClaude

本地 AI Agent 项目。`dev` 用于逐步整合各阶段的代码修复与个人改进，完成验证后再合并到 `main`。

当前已整合 S0：CLI 与 Core 双进程通信、TCP 上的 JSON-RPC 请求与响应、NDJSON 分帧、配置与日志，以及 Windows 下的前台启动和 Ctrl+C 退出适配。模型调用、工具执行和 TUI 等功能尚待后续阶段整合。

## 环境要求

- Python 3.12
- uv
- Windows、macOS 或 Linux；本次 Windows 适配覆盖当前 S0 功能

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

默认监听 `127.0.0.1:7437`。当前功能无需模型 API key；配置方式见 [RUNBOOK.md](./RUNBOOK.md)。

## 开发验证

```text
uv run pytest tests/ -v
uv run ruff check src tests scripts
uv run mypy src
uv run python scripts/gen_protocol_doc.py --check
```

## 阶段整合约定

各阶段分支保留各自的学习用 README 和 `docs/learning/`。`dev` 独立维护本 README，不跟踪阶段学习文档；协议与运行文档继续随代码维护。

后续合并阶段代码时，应在提交合并结果前检查差异，保留 `dev` 的 README，并排除新增或修改的学习文档。`.gitignore` 只避免未跟踪的学习文件被误加入，不能阻止其他分支已跟踪的文件通过合并进入 `dev`，因此每次合并都需要检查。

学习文档仍保存在阶段分支的历史中。由于 `dev` 从已有 S0 提交创建，早期提交也包含这些文档；本分支当前版本不再跟踪它们，不重写已有历史。

## 项目文档

- [RUNBOOK.md](./RUNBOOK.md)：配置、开发命令与故障排查。
- [WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)：由代码生成的 IPC 协议说明。
