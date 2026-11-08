# KamaClaude 的 RepoQA 函数定位评测

Agent 在数据集自带的源码快照中自行搜索，只收到题目的自然语言描述。判分要求相对路径、函数名、所在定义行范围同时匹配。每题只有一次最终提交机会。这套协议测量 Agent 的函数定位能力，RepoQA 官方榜单采用预先构造长代码上下文和代码相似度判分，两个分数应分开报告。

在 KamaClaude 根目录打开 PowerShell。默认使用项目配置中的 DeepSeek 模型和 `.env` 里的凭据。

```powershell
$data = 'C:\Users\hmy\Desktop\repoqa-2024-06-23.json\repoqa-2024-06-23.json'
$python = '.\.venv\Scripts\python.exe'

# 只准备一份快照和题目，不调用模型
& $python scripts/eval_repoqa.py --dataset $data --repos-per-language 1 --prepare-only

# Python 单题，两组各跑一次
& $python scripts/eval_repoqa.py --dataset $data --repos-per-language 1

# Python 10 个仓库各抽 3 题，两组各 30 次 Agent Run
& $python scripts/eval_repoqa.py --dataset $data --language python --per-repo 3
```

基线组提供目录、文件、文本搜索及读取工具。符号组增加文件大纲和符号读取。两个组共用题目、模型、上下文配置和执行预算。默认每题最多 20 步、120 秒。脚本直接启动独立 Runner，串行执行，不需要 Core 或 TUI。符号工具会沿用 KamaClaude 对各语言的实际支持范围。

`--language` 可以一次指定多种语言，也可以传 `all`。`--mode baseline` 或 `--mode symbols` 可单跑一组。`--per-repo 10 --language all` 在这版数据上会选 600 道题，两组共 1,200 次 Agent Run。每个 Run 可能包含多次模型请求。

默认输出到 `runs/repoqa/` 下的新目录，也可以指定一个尚不存在的 `--output` 路径。

- `manifest.json` 保存选题、数据 SHA-256、配置、工具清单和预期结果，位于 Agent 工作区之外。
- `results.jsonl` 每完成一次 Run 追加一条记录，包括原始答案、判分、耗时、工具调用数和已收到的 Usage。
- `summary.json` 按组和语言汇总准确率、耗时和用量。超时、运行失败和无效答案计入失败。中途停止时，报告会区分计划数与已有结果的数量。
- `agent-runs/` 保存每题的事件记录，可回看具体搜索步骤。结束提交工具 `finish` 不计入仓库工具调用数。没有 Usage 事件的题，逐题记录中的 Token 字段为空。

连续三次模型调用失败时批跑会暂停。恢复后，将原命令的参数连同 `--output <原运行目录> --resume` 一起传入。脚本沿用原清单，重试模型调用失败的 Run，并继续尚未完成的 Run。`results.jsonl` 保留全部尝试，`summary.json` 按题目和组别取最近一次结果。

对照实验使用同一个数据文件和 `--seed`。记录实验时的 KamaClaude 代码版本与模型名，并以 `completed == planned` 检查两组是否跑全。
