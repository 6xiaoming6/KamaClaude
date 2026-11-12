# Run 级 Docker 命令沙箱

## 1. 实现什么

默认把 `bash` 从宿主命令改成 Docker 内的 Linux Bash。一个 Run 持有一个懒创建的容器，连续工具调用复用；Run 退出后删除。Core、模型请求、文件工具、记忆和 MCP 仍在原有位置运行，没有把整个应用搬进容器。

```text
SessionManager 登记后台 Run
  → Runner 创建执行器对象（此时不访问 Docker）
  → Loop 请求模型，模型调用 bash
  → 参数检查、真实 Shell 审批、保存 executing
  → 第一次：检查本机 Linux 引擎及已有镜像，创建并启动容器
  → 后续：docker exec 在同一个容器中执行 Bash
  → 保存工具结果、推进游标
  → Runner finally 删除容器，再发送运行收尾通知
```

之前 `CommandExecutor` 每次启动宿主 Shell，只固定 cwd，不能限制命令访问其他宿主目录。现在保留它作为显式 host 后端，并新增独立的 Docker 执行器。两者返回同一种 `CommandResult`；BashTool 负责转换为 ToolResult，Loop 不包含 Docker 管理逻辑。

## 2. 安装与配置

Windows 使用 Docker Desktop 的 Linux 容器模式。其他平台也只支持本机 Unix socket 连接的 Linux 引擎；远程 daemon、TCP/SSH Docker 连接和 Windows 容器会明确拒绝，避免把本地路径挂到另一台机器。

从仓库根目录显式构建：

```powershell
docker build -t kamaclaude-sandbox:py312-node24 -f docker/sandbox/Dockerfile docker/sandbox
```

镜像包含 Bash、Git、Python 3.12、pip、pytest、Node.js 24、npm。构建上下文只包含 Dockerfile，不发送项目工作区；运行时使用已有镜像 ID 创建容器，不隐式拉取或构建。

```toml
[execution]
backend = "docker"
forward_env = []
env = {}

[execution.docker]
image = "kamaclaude-sandbox:py312-node24"
cpus = 2
memory_mb = 2048
pids_limit = 128
```

对应环境变量为 `KAMA_EXECUTION_BACKEND`、`KAMA_DOCKER_IMAGE`、`KAMA_DOCKER_CPUS`、`KAMA_DOCKER_MEMORY_MB`、`KAMA_DOCKER_PIDS_LIMIT`，覆盖 TOML。环境变量值和镜像缺失不会触发 host 回退。

显式需要本机执行时：

```powershell
$env:KAMA_EXECUTION_BACKEND = "host"
uv run kama-core
```

这会使新 Run 使用 Windows cmd 或 POSIX `/bin/sh`。已有 Run 沿用非敏感执行配置快照；升级前的检查点没有该字段时按原 host 环境处理，冻结 Schema 校验不放宽。工具描述已经变化的旧中断 Run 可能需要先终止，再发起新 Run。

## 3. 生命周期与状态

| 情况 | 行为 |
|---|---|
| Run 不调用 Bash | 不访问 Docker，不创建容器 |
| 同一 Run 再次调用 Bash | 使用原容器，每次启动独立 Bash，cwd 为 `/workspace` |
| TUI 临时断线 | Core 持有的 Run 和容器继续工作 |
| 正常结束、失败、取消、保存错误 | Runner 的 finally 回收本轮容器 |
| 用户安全停止 | 当前工具自然结束后停在安全边界，再清理容器 |
| 命令超时、取消或执行失联 | 销毁容器，副作用不明时保留 unknown，仍需人工确认 |
| Core 硬退出 | 容器可能遗留；同一归属的 Core 下次以 Docker 模式启动时检查并回收 |
| 恢复原 Run | 创建新执行器及新容器，completed 工具不重跑 |

同一容器中，工作区文件、用户目录中的依赖、`/tmp` 文件可以跨调用使用。每次命令是独立进程，`cd`、`export` 和虚拟环境激活状态不会自动带到下一次。Python 用户包安装到 `/home/kama/.local`，npm 可使用工作区或用户目录。

恢复仅保留挂载工作区中的内容，不保留容器内存、进程或临时依赖。检查点冻结的是后端、镜像引用和资源配置，不是完整环境镜像；手工重建同名镜像也不能宣称环境完全一致。Windows 的 `.venv` 不能直接当成 Linux 虚拟环境使用。

子 Agent 按自己的 Run 持有不同容器，与父容器独立回收；其挂载工作区仍按现有规则共享，这不是单写者协调或独立代码分支。

## 4. 隔离与资源边界

- 只有本轮 workspace 读写挂载到 `/workspace`，目录由后端解析，模型不能指定挂载参数。
- 根文件系统只读，移除 capabilities，禁止提权，使用默认 seccomp 和非 root 身份。
- Windows 使用 UID/GID 1000；POSIX 尽量匹配非 root 宿主用户。以 root 启动 Core 的 Linux 用户需自行保证工作区对容器非 root 身份可访问，不自动修改宿主权限。
- 默认每容器 2 CPU、2 GiB 内存，内存和 swap 总上限相同、128 进程；另有 256 MiB `/tmp` 和 512 MiB 用户目录的 tmpfs，它们同样消耗容器内存。并发容器分别计额度，不是全树共享资源总上限。
- bridge 允许联网，不映射端口、不使用 host 网络、不挂 Docker socket；仍可能访问可达的局域网服务，且不阻止向外上传内容。
- 宿主工作区真实文件可修改或删除，未提供回滚、磁盘写入配额或恶意代码的强安全保证。

创建时把工作区根目录已有的 `.env`、`.env.*` 用只读空文件遮蔽，`.env.example` 保留；不会改写宿主原文件。Windows 名称按系统大小写规则判断。凭据路径是链接或非普通文件时拒绝启动，避免错误挂载。

这不是递归秘密扫描：其他名字、嵌套目录、别名、运行期间新增文件和实际打印在输出中的秘密都不在本版完整保护范围。容器环境仅接收显式允许的值，并排除宿主 PATH、HOME、模型 API Key、`KAMA_*` 与 `DOCKER_*`。Docker CLI 的连接配置只留在宿主进程。

## 5. 错误、审批与清理

Docker 模式使用 Bash AST，即使宿主是 Windows；host 模式继续按宿主 Shell 判断。后端信息来自工具实例，不接受模型伪造的 platform 或挂载字段。审批后新增的禁止规则仍用相同 Shell 复核，`full` 也不能跳过明确禁止。

容器准备最多 30 秒；命令保留 1～120 秒期限和 64 KiB 输出上限，超过上限继续排空管道。取消或超时后，只杀 Docker CLI 不足以清理，执行器会另行删除容器。删除最多等待 5 秒，失败通过日志和现有 `log.line` 事件报告，不宣称已经清理，也不会降级已提交的成功终态来重执行业务。

镜像、引擎、挂载等准备失败发生在用户命令发出前，是已知配置错误。执行后无法确认结果则进入既有 unknown 流程。清理是尽力且有界的，不宣称能在 Docker 服务失效时强制删除容器。

容器标签包含 Core 归属、宿主 PID 和创建时间、Run ID 与执行尝试 ID。启动扫描只考虑本 Core 归属，并检查宿主进程身份；活跃进程、其他归属、缺失或无效标签保守跳过，避免误删。扫描自身也有时间上限，失败明确记录。

## 6. 阅读与验证

推荐顺序：

1. [BashTool](../src/kama_claude/core/tools/builtin/bash.py)：参数、真实 Shell 信息和结果转换。
2. [执行模块](../src/kama_claude/core/execution/)：工厂选择、Docker CLI、容器复用及归属清理。
3. [Runner](../src/kama_claude/core/runner.py) 与 [子 Agent](../src/kama_claude/core/subagent/tool.py)：组装注入与 finally 收尾。

```powershell
# 确定性测试：不要求 Docker 可用
uv run pytest tests/unit/test_docker_execution.py -q

# 真实 Docker：先启动引擎并构建镜像
uv run pytest tests/integration/test_docker_sandbox.py -q
```

真实用例验证挂载、凭据遮蔽、非 root、只读根目录和资源参数，实际安装小型公开 Python/npm 依赖并跨调用使用，验证超时/取消清理，以及真实 Core/TCP 断线与硬重启恢复。模型由确定性替身替代，不调用付费模型。不具备引擎或镜像时明确跳过，不能把替身测试当作真实隔离验证。

本轮已显式构建开发镜像，真实 Docker 六项全部通过；其余非付费回归为 1,376 项通过、2 项跳过，分组执行合计 1,382 项通过。Ruff、完整源码 mypy、协议一致性和文档本地链接检查通过。过程中 Docker API 曾临时超时，恢复后重新完成了六项真实用例；没有通过重启引擎或清理其他任务来掩盖失败，也没有调用付费模型。
