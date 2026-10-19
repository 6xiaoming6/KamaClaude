# S1 Agent 最小闭环

基准：`origin/stage/s1`，提交 `76e2992`。本阶段把用户目标、模型调用、工具执行、结果回填和终止条件串起来。最重要的事实是：此时 `kama run` 直接创建 `AgentRunner`，Agent 在 CLI 进程内执行。

## 1 从聊天程序到 Agent 的区别

普通聊天是“输入 → 模型 → 文本”。这里多了一种模型输出：结构化工具调用。模型返回工具名称、参数和调用 ID，程序执行后把结果放入消息历史，再次请求模型。这个循环使模型能够根据真实观测调整行动。

所谓 plan、act、observe，在代码中主要对应模型提出动作、宿主执行动作、下一次请求看到动作结果。项目没有一个独立的规划模型，也没有读取模型内部推理过程。可以称为 ReAct 风格的工具循环，不能把终端上的 `planning...` 当成已经实现了复杂规划器。

## 2 先读这七个位置

| 文件及符号 | 负责的事情 |
| --- | --- |
| `cli/commands/run.py::cmd_run` | 创建 StdoutPrinter 和 Runner |
| `core/runner.py::AgentRunner.run` | 创建运行目录、总线、Provider、工具和上下文 |
| `core/loop.py::AgentLoop.run` | 多步循环及终止判断 |
| `core/context.py::ExecutionContext` | 消息历史、步数和运行状态 |
| `core/llm/provider.py::AnthropicProvider.chat` | SDK 流转成内部响应与事件 |
| `core/tools/invocation.py::invoke_tool` | 工具查找、简单校验、超时与错误处理 |
| `core/events/bus.py` 和 `writer.py` | 订阅和事件落盘 |

路径相对 `src/kama_claude/`。先顺着调用链读，再回看类型定义，比按目录字母顺序阅读有效。

## 3 Runner 和 Loop 为什么要分开

Runner 管理“一次运行所需的资源”：生成 `run_id`，创建 `runs/<run_id>/`，打开 `events.jsonl`，注册订阅者，发布开始和结束事件。Loop 只关心“下一步做什么，是否结束”。

这样测试 Loop 时只需要 FakeProvider、测试工具和 EventBus，不必真的创建 daemon 或终端。测试 Runner 才验证目录、文件和生命周期。后面 S2 把 Runner 搬进 Core 时，循环主体可以基本保持不变。

`LLMProvider` 使用 `Protocol`，核心要求是有符合签名的异步 `chat()`。Runner 允许注入 provider，测试据此用固定响应替换网络请求。这是依赖注入的实际价值：控制输入，稳定验证分支。

## 4 用一个读文件任务走完消息变化

目标：“读取 `README.md` 并说明它的用途。”初始上下文：

```json
[{"role":"user","content":"读取 README.md 并说明它的用途"}]
```

第一步模型选择工具，内部表示为：

```text
LlmResponse(
  stop_reason="tool_use",
  tool_calls=[ToolCallBlock(id="call-1", name="read_file", input={"path":"README.md"})]
)
```

Loop 先加入 assistant 消息，再执行工具。结果历史如下，正文为教学简写：

```json
[
  {"role":"user","content":"读取 README.md 并说明它的用途"},
  {"role":"assistant","content":[{"type":"tool_use","id":"call-1","name":"read_file","input":{"path":"README.md"}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"call-1","content":"# KamaClaude ..."}]}
]
```

第二步模型收到上述历史，生成总结并返回 `end_turn`，Loop 设为成功。工具结果采用 `role=user` 是本项目使用的消息格式约定，不表示用户亲自输入了这些内容。

关键不变量是 `tool_use.id` 与 `tool_result.tool_use_id` 对应。一次模型响应可以包含多个工具调用；S1 用 `for` 逐个 `await invoke_tool()`，因此这些调用按顺序执行。`add_tool_result()` 把同一步多个结果合并进同一条 user 消息，保持消息结构。

## 5 模型适配层到底做了什么

`AnthropicProvider.chat()` 接收 messages、tool_schemas、bus、run_id。它构造模型请求，设置 S1 的输出上限 `max_tokens=4096`，添加 system prompt，把工具注册表转换出的 schema 传给 SDK。

它边消费 `stream.text_stream` 边发布 `LlmTokenEvent`，同时累积文本。流结束后调用 `get_final_message()`，从最终结构提取工具调用、停止原因和 usage。屏幕增量文本是展示通道；最终结构化响应才决定接下来执行什么。

名称里的 token 不保证每个事件严格等于模型 tokenizer 的一个 token；代码发送的是 SDK 产生的文本片段。事件次数不能直接当成计费 token 数，应看 usage。

System 和最后一个工具 schema 加了 `cache_control` 标记；实际缓存命中取决于所接服务返回的 usage，不能仅凭设置标记宣称有固定的费用收益。S1 没有动态模型路由，每次使用配置的默认模型。

## 6 工具执行的统一入口

`ToolRegistry` 用名称映射 `BaseTool` 实例。同名注册会覆盖旧工具。`tool_schemas()` 把 name、description、input_schema 提供给模型。

`invoke_tool()` 的 S1 顺序是：

```text
发布 tool.call_started
→ registry.get(name)
→ 检查 input_schema.required 中的字段是否存在
→ wait_for(tool.invoke(...), timeout=10 秒)
→ 成功发布 tool.call_finished
→ 失败发布 tool.call_failed 并返回 is_error=True
```

S1 只检查 required 字段是否存在，不检查参数类型、范围或嵌套结构。模型输出 JSON 不意味着参数一定安全正确。

工具失败通常不终止整个 run。例如文件不存在，错误会作为 `tool_result` 返回模型，下一步可以解释错误或选择其他路径。模型 API 失败则导致 `llm_error` 并停止，二者所在层次不同。

取消也不能与普通失败混淆：Loop 捕获 `CancelledError` 后记录 cancelled 并重新抛出；Runner 尝试发布 run.finished，再继续传播取消。这让外层保留取消语义。

## 7 ReadFileTool 的真实限制

它检查路径组成部分中有没有 `..`，然后 `Path.read_bytes()`，最多把前 512 KiB 解码后返回，超出时加标记。

三个细节值得记住：

1. “描述要求相对路径”不等于实现拒绝绝对路径。S1 没有规范化后检查是否仍在工作目录内。
2. 符号链接可能指向目录外，禁止 `..` 不能提供完整目录隔离。
3. 先 `read_bytes()` 再截断，是限制返回内容，仍会先把整个文件读入内存；而且同步磁盘读取会阻塞当前事件循环。

这些都是从实际实现得到的改进起点，不需要先引入复杂框架。

## 8 事件和状态机

典型事件顺序：

```text
run.started
  step.started
  llm.model_selected
  llm.token ...
  llm.usage
  tool.call_started
  tool.call_finished 或 tool.call_failed
  step.finished
  step.started
  ...
run.finished
```

若模型调用中抛异常，代码直接 break，此步不一定出现 `step.finished`。因此消费者不能假定每个 started 都已有严格配对的 finished。

`ExecutionContext.status` 初始 running；`end_turn` 设置 success；步数用尽设置 failed，reason 为 exceeded_max_steps。终止判断里 `end_turn` 优先，所以在最后允许的一步正常回答仍然成功。

这里 success 的含义是模型正常结束。它不自动证明用户目标在业务上达成，也不检查所有任务文件是否完成。这正是以后引入结果验收的重要理由。

EventBus 按注册顺序逐个 `await handler(event)`。它是进程内串行分发器，没有队列、隔离、重试或持久化保证。EventWriter 每条 write 后 flush，并吞掉写盘错误记录日志；flush 也不是崩溃后必定持久化的承诺。

## 9 实验和测试阅读

S1 不需要启动 daemon 就能运行：

```powershell
uv run kama run --goal '读取 README.md 并说明用途'
uv run pytest tests/unit/test_context.py tests/unit/test_loop.py tests/unit/test_invocation.py tests/unit/test_runner.py -v
```

真实模型运行需要有效配置；单元测试用 FakeProvider。打开新生成的 `runs/<run_id>/events.jsonl`，按 run_id 和 tool_use_id 画出执行顺序。不要期待 S4 的 session 目录已经存在。

重点读 `test_tool_use_then_end_turn_marks_success`、`test_tool_failure_loop_continues_to_success`、`test_max_steps_marks_failed`。它们分别证明闭环、可恢复工具失败、循环上限。

练习一：让 FakeProvider 首次返回 read_file，第二次返回 end_turn，断言第二次收到工具结果。练习二：始终返回工具调用，设置 max_steps=2，验证恰好两步后失败。练习三：添加一个只计算字符串长度的工具，用不含外部副作用的例子练习注册与 schema。

## 10 面试追问

**模型真的执行了工具吗？** 模型提出结构化请求，宿主查找工具实现并执行，结果再回填模型。

**为什么工具失败不直接退出？** 失败也是观测，模型可以据此调整；但必须有步数或预算限制，避免无限尝试。

**为什么需要两个 ID？** run_id 标识完整运行，tool_use_id 标识一次工具请求与结果的配对；一个 run 有多个工具调用。

**怎样在没有 API key 时测试？** 注入实现相同 chat 接口的 FakeProvider，预定义响应和异常，并检查传入消息及事件。

## 11 核心方法逐个精读

### 11.1 cmd_run 与 StdoutPrinter

`cmd_run(goal, config)` 接收 CLI 解析好的目标和配置，创建 StdoutPrinter，把其 handle 方法放入 Runner 的 extra_handlers，再 asyncio.run(runner.run(goal))。S1 没有经过 SocketClient，这一行就是判断运行所在进程的最直接证据。

`StdoutPrinter.__init__()` 记录当前是否正在同一行打印 token、运行开始时间。`_ensure_newline()` 在有未结束文本行时补换行；`handle(event)` 依据事件类打印开始、模型文本、工具和终态。它改变展示状态，不修改 ExecutionContext。

注意 handle 是 async，但里面 print 仍然是普通输出操作。这里用异步签名是为了符合 EventHandler 接口，不表示打印自动在别的线程运行。

### 11.2 AgentRunner.__init__

构造器保存 config、可选 provider、额外事件处理器、可选 runs_dir。它主要保存依赖，没有开始模型推理。provider 不传时以后才创建真实 AnthropicProvider；测试传 FakeProvider 后不需要真实网络。

`extra_handlers or []` 表示没有提供有效列表时用空列表。默认运行目录来自 RUNS_DIR。依赖从构造器传入，能让测试把文件写进 tmp_path，而不是污染真实运行目录。

### 11.3 AgentRunner.run

这是本阶段的装配中心。输入 goal，返回 None，主要效果是创建目录、执行循环和写事件文件。步骤是生成 run_id；创建运行目录；建立 EventBus；注册展示 handler；准备 Provider 和只包含 read_file 的 Registry；创建 Loop 和 Context；进入 EventWriter 的 async with；发布 run.started；await loop.run；发布 run.finished。

取消分支保存 cancelled 标志，尽可能写终态后再重新抛 CancelledError。不要把这理解成任意初始化失败都会被捕获：S1 的 Provider 构造位于运行循环的 try 外，缺 key 会 SystemExit，未形成统一终态。

学习时用不同颜色圈出三类代码：依赖装配、业务执行、清理/记录。以后需要换 UI 时通常改订阅者，需要换模型时改 Provider，需要增加工具时改 Registry，而不是把所有逻辑塞进 Loop。

### 11.4 ExecutionContext.__post_init__

dataclass 生成初始化代码后调用这个方法。若 messages 为空，就把 goal 放成第一条 user 消息；若调用者已给消息，则保留已有内容。返回 None，但对象消息列表改变了。

`goal` 是运行目标字符串，`messages` 是模型实际看到的序列，两者相关但不是同一个东西。S4 引入历史预填后，要关注究竟哪条路径构造 messages。

### 11.5 add_assistant_message

输入 content 是这次模型返回的内容块列表。方法追加 `{"role":"assistant","content":content}`。它不向模型发请求，不执行内容中的工具，也不自动保存到文件；只改变当前内存历史。

为什么要先追加 assistant 的 tool_use 再追加结果？结果需要说明自己回应哪次调用。若只有 tool_result，没有提出动作的消息，模型无法得到一条完整的工具交互记录。

### 11.6 add_tool_result

输入 tool_use_id、content 和 is_error，先建立一个工具结果 block。若 is_error 为 True 才加入相应错误字段。随后查看最后一条消息：如果已经是只含工具结果的 user 消息，追加 block；否则新建一条 user 消息。

例如模型一次请求 A、B 两个工具，执行 A 后建 user 列表，执行 B 后合并进去。这不是把两份输出拼成一个字符串，而是在同一条消息中保留两个带不同 ID 的结构化 block。

检查 `last`、role、content 是否是列表、列表非空、所有 block 类型，都是为了避免把工具结果塞入普通用户文本或不兼容结构。

### 11.7 is_done mark_success mark_failed

`is_done()` 判断 status 是否不是 running；`mark_success()` 设置 success；`mark_failed(reason)` 设置 failed 并记录原因。这些方法很短，却集中定义了 Loop 的状态出口。

它们不会取消已经启动的外部进程，也不写文件。状态更新只是状态更新，执行资源清理需要其他所有者负责。把小方法理解清楚，有助于避免把字段变化当成已经完成系统级操作。

### 11.8 AgentLoop.__init__

只保存 Provider、Registry 和 EventBus 三个依赖。Loop 不负责自行寻找 API key，也不扫描磁盘加载工具，因此可以使用任何符合接口的测试替身。

Registry 告诉模型和执行器“能做什么”，Provider 决定“如何向模型提问”，Bus 把“发生了什么”交给外部。三种职责通过对象协作，而不是依靠全局变量。

### 11.9 AgentLoop.run 的第一部分

while context 未结束时，先 step 加一并发 step.started，再调用 provider.chat，参数包括当前 messages 和 registry.tool_schemas。注意每一步都重新提供完整当前消息，不是只把最新工具输出发过去。

模型异常会设置 llm_error 后 break；取消会设置 cancelled 并再次抛出。正常得到响应后，先收集文本 block，再收集 tool_use block，再 add_assistant_message。

`response.stop_reason` 是控制后续分支的关键字段。文本里说“我要读取文件”但没有合法 tool_use，并不会自动触发文件工具。

### 11.10 AgentLoop.run 的第二部分

stop_reason 为 tool_use 时，按顺序遍历每个 ToolCallBlock，await invoke_tool，然后 add_tool_result。一次工具失败仍能得到 ToolResult，所以消息循环通常继续。

随后若 end_turn 则 success；否则步数达到 max_steps 则 exceeded_max_steps。最后发 step.finished。while 再判断状态，决定是否继续。S1 没有“任务列表全部完成才成功”的额外校验，也没有每个工具并行 gather。

你可以把此方法拆成四个框来复述：请求模型、记模型响应、执行并记工具结果、判断结束。这比逐句背代码更能迁移到别的 Agent 项目。

### 11.11 AnthropicProvider.__init__

输入 model 和可选 client。测试传入 client 时绕过真实 SDK 客户端创建；否则读 ANTHROPIC_API_KEY，缺失就终止初始化，有值才创建 AsyncAnthropic。模型名保存到 `_model`。

这两个注入点层次不同：Runner 可以替换整个 Provider，Provider 自己可以替换底层 SDK client。前者测试业务循环，后者测试 SDK 事件到内部格式的转换。

### 11.12 AnthropicProvider.chat

先发布模型选择事件，再准备 system、工具 schemas、messages 和输出上限。进入 SDK 流时，用 async for 逐段取文本，发布 llm.token 并累积 text_parts；结束后取得 final_message。

接着读取 usage，缺少缓存字段时使用 0；发布 llm.usage；遍历 final_message.content，只把 tool_use 块转换成 ToolCallBlock；最终返回 LlmResponse。

输入是宿主消息，返回是整个响应对象；过程事件只负责展示与观测。S1 返回的 text 来自增量拼接，工具参数来自最终结构。你调试工具没执行时，应检查最终 tool_calls 和 stop_reason，不能只看屏幕文本。

### 11.13 ToolRegistry 的三个方法

`register(tool)` 把工具保存到名称字典；`get(name)` 返回实例或 None；`tool_schemas()` 遍历已注册实例，把 name、description、input_schema 组为面向模型的列表。

描述与 schema 使模型知道怎样提出请求，工具实例提供真正执行能力。仅给模型看一个工具描述、却没注册可执行实例，会在统一调用处得到 unknown tool。

### 11.14 invoke_tool

参数包含 Registry、模型给的 ToolCallBlock、Bus、run_id 和默认 10 秒超时。它先记起始 monotonic 时间并发布 started，然后 get 工具。查不到就 `_fail`；缺 required 参数也 `_fail`；通过后 wait_for 调用 tool.invoke。

正常 ToolResult 还可能带 is_error=True，调用层会把它转换成失败事件；真正成功则发布 finished 并返回原结果。TimeoutError 与其他 Exception 也转换成带错误信息的 ToolResult。

“不向 Loop 抛普通工具异常”让 Loop 得以把错误交给模型。但并非所有来源异常都被这个 try 包住，例如前面的事件发布出错仍可向外传播；取消也保留其特殊语义。

### 11.15 _fail 与 ReadFileTool.invoke

`_fail()` 接收错误类别、消息和耗时，发布 ToolCallFailedEvent，再返回 `ToolResult(content=错误消息,is_error=True,...)`。它把 UI 诊断与模型可见结果同时准备好，避免不同错误分支漏其中一项。

`ReadFileTool.invoke(params)` 取 path，检查 `..`，read_bytes，切前 512 KiB，UTF-8 解码时用 replace 处理坏字节，再返回 ToolResult。FileNotFoundError 等交给统一层处理。没有输出时可能得到空字符串，不能把空内容自动当读取失败。

### 11.16 EventBus.subscribe 与 publish

subscribe 保存回调引用，不立即调用。publish 接收事件对象，按订阅顺序逐一 await handler。同一个事件对象会交给所有 handler，而不是自动复制给每人。

不含 timeout、不隔离 handler 异常、不负责磁盘。名字叫 Bus 只是集中分发入口，具体可靠性要看实现。写一个 handler 故意抛错，就能观察后续 handler 是否还会执行。

### 11.17 EventWriter 的资源与处理方法

构造器只保存路径、将文件句柄设为 None。`__aenter__` 创建父目录，追加打开 UTF-8 文件并返回 self；`subscribe(bus)` 注册自己的 handle；`handle(event)` 在文件开启时序列化一行并 flush；`__aexit__` 关闭句柄并设回 None。

async with 保证退出块时调用清理方法，减少忘关文件的可能。但是 __aexit__ 没有从 EventBus 移除 handle，S2 共享总线后这个细节就开始影响长期生命周期。

### 11.18 运行 ID 与路径辅助函数

`new_run_id()` 使用 UTC 时间和 UUID 短后缀，便于人工识别又降低碰撞概率；`run_dir(id)` 组合运行目录；`events_file(id)` 定位事件文件；`ensure_run_dir(id)` 创建并返回目录。路径拼接不等于输入校验，未来从外部接收 ID 时还要限制格式。

它们只处理名称和位置，不代表运行已存在或成功。拿到 run_id 后，仍需读取对应状态或事件判断结果。

## 12 手工执行一段固定响应

假设 FakeProvider 第一次返回 read_file 的工具调用，第二次返回 end_turn。请手填下面表格，再对照第 4 节的消息示例：

| 时点 | step | status | messages 中新增内容 |
| --- | --- | --- | --- |
| 初始化 | 0 | running | 用户目标 |
| 第一次调用前 | 1 | running | 无 |
| 收到工具请求 | 1 | running | assistant 的 tool_use |
| 工具返回 | 1 | running | user 的 tool_result |
| 第二次模型回答 | 2 | success | assistant 的最终文字 |

现在把 read_file 改为抛 FileNotFoundError。正确推演是第三条变成错误 tool_result，step 仍可进入 2。再把 Provider 改为抛网络异常，则 Loop 会以 llm_error 结束，不会产生那个工具结果。这两种失败路径的区别必须能自己解释。

## 13 本阶段可改进的地方

| 改进 | 具体实现方向 | 验收方法 |
| --- | --- | --- |
| 文件读取边界 | 解析工作区根目录和目标真实路径；拒绝越界；限量读取 | 绝对路径、`..`、链接越界、大文件分别验证 |
| 订阅者故障隔离 | 区分关键持久化 handler 和可降级显示 handler | 显示器抛错不影响执行；关键存储错明确使 run 失败 |
| 完整终态 | 初始化失败和取消也进入统一 try/finally | 每个已接受 run 最终有一个可查询终态 |
| 工具参数模型 | 从 required 检查升级到参数类型和范围校验 | 非字符串路径在执行文件操作前被拒绝 |
| 业务验收 | 将模型 end_turn 与外部验证结果分开 | 模型称已写文件而文件不存在时不能报告已验收 |

S5 已加入部分参数模型与失败处理。若在最终分支做改进，应先检查现有实现，避免把已存在的功能重复包装成个人创新。
