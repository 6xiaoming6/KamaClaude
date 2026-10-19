# S7 Skills 子Agent与MCP

基准：`origin/stage/s7`，提交 `70decaf`。main 的业务源码与该阶段相同。本阶段增加工作流模板、子 Agent 和外部工具适配，三者解决的是不同问题。

## 1 三种扩展机制的区别

| 机制 | 扩展什么 | 是否创建新执行上下文 | 关键入口 |
| --- | --- | --- | --- |
| Skill | 提示模板与工具白名单 | 通常沿用当前会话运行 | `/review ...` 等斜杠输入 |
| Subagent | 把独立子任务交给另一轮 AgentLoop | 是，冷启动消息上下文 | spawn_agent |
| MCP | 接入其他服务提供的工具 | 不必新建 Agent | 外部工具名 |

Skill 不是模型训练；子 Agent 不是默认独立进程；MCP 不是让模型直接执行网络请求。宿主仍负责装配、权限和真正调用。

## 2 代码阅读顺序

先读 `core/skills/loader.py` 和 `core/session/manager.py::send_message`，观察用户输入如何变成运行配置；再读 `core/runner.py::_build_registry`，观察白名单如何落实；然后读 `core/agents/loader.py`、`core/subagent/tool.py`、`registry.py`；最后看 `core/mcp/client.py`、`tool.py`、`server.py` 及 Core 启停接线。路径相对 `src/kama_claude/`。

## 3 Skill 的查找与解析

Skill 包含 name、description、system_prompt_template、allowed_tools。加载顺序是项目 `.kama/skills`、用户 `~/.kama/skills`、包内 builtin；每级既支持 `<name>.md`，也支持 `<name>/SKILL.md`。先命中的项目版本可以覆盖内建同名 Skill。

示例文件：

```markdown
---
name: explain
description: 解释指定文件的调用链
allowed_tools:
  - read_file
  - list_dir
---
请解释 $ARGUMENTS 的入口、依赖、状态变化和错误处理，并给出一项练习。
```

解析器是自写的有限 frontmatter 解析，不是完整 YAML 引擎。它能处理简单键值和部分块标量，但把前置区域中的 `- ` 行直接收集进 allowed_tools，缺少严格字段范围。格式错误可能被静默忽略，需要改进错误提示。

`render_prompt()` 用字符串替换 `$ARGUMENTS`。本地和用户 Skill 来自文件，属于影响模型行为和可见工具的配置；查找名称也应做边界校验，不能默认任意路径片段都可信。

## 4 Skill 从解析到模型请求的实际缺口

SessionManager 先把原始 `/explain file.py` 写进 thread，再识别 Skill，生成渲染后的 goal，设置 system_prompt_override 为原始模板，设置工具白名单。

Runner 有 session 时优先读取 thread；ExecutionContext 有 prefill_messages 时不会再追加渲染后的 goal。因此渲染后的 `$ARGUMENTS` 替换文本未必真正进入模型 messages，system override 又是未替换的模板。模型可能仍通过原始斜杠输入理解参数，但这不是可靠的模板传参实现。

另外，用户只输入 `/` 时 `parts` 为空，却访问 `parts[0]`，会导致异常。Unknown Skill 目前会继续作为普通文本执行，是否希望这样应在产品语义中明确。

正确验收方式是让 FakeProvider 捕获 messages 和 system，断言最终实际请求含展开后的参数，而不是只测试 render_prompt 的返回值。

## 5 工具白名单的强度与边界

Runner `_build_registry()` 通过 `_ok(name)` 判断是否注册工具。若白名单中没有 write_file，模型 schema 里看不到它，registry 中也没有它，所以强度高于单纯在 prompt 里说“不要写文件”。

但空列表被当作“不限制”，不能表达“禁止全部工具”。并且如果允许 spawn_agent，子 Agent 按自己的 profile 重建工具表，没有自动与父级白名单取交集，可能获得父级未直接拥有的能力。

内建 reviewer 的 prompt 强调只读，但 allowed_tools 仍包含 bash。bash 本身具备写入能力，即使需要审批，也不能说这个角色具有硬只读隔离。应该从实际能力而非角色名称判断边界。

## 6 子 Agent 的执行过程

`SpawnAgentTool.invoke()` 校验 description、prompt、run_in_background、subagent_type。若有角色名，加载 TOML profile，得到 system_prompt、allowed_tools、model。

接着创建新的 run_id 和 ExecutionContext，goal 只包含传入 prompt，没有父会话历史、notes、项目 context 的自动继承。它创建 child_bus，并通过 bridge 把子事件转发到 parent_bus。

独立 TaskManager 放在子 run 的 `.tasks` 目录；子工具表包含选定的本地文件和任务工具，并在深度允许时注册继续 spawn 的工具。它没有自动接入父 Runner 的 MCP 工具或 NoteSaveTool，也没有传入父级 Compactor。

前台模式在工具调用中 await child_loop，结束后返回文字结果；后台模式 create_task，注册 task 与 context，然后立即返回 run_id，父模型以后用 agent_result 查询。

本实现通常允许根 Agent 派生子 Agent，子 Agent 再派生一层；最深层不再注册 spawn。还有 depth>=2 的防御性检查。深度上限只限制嵌套，不限制同一层创建多少个后台任务。

## 7 隔离和生命周期要看哪些边界

子 Agent 隔离的是消息上下文和部分运行文件，不隔离 OS 权限或工作目录。它仍与父级共享 Provider、PermissionManager、session_id 和实际文件系统。子任务写同一文件仍可能冲突。

Profile 的 model 字段被加载，但 SpawnAgentTool 继续使用传入的同一个 Provider，没有根据 profile.model 新建 Provider。因此“planner 用小模型、executor 用大模型”在这里还只是配置意图，尚未接通。

BackgroundTaskRegistry 在 AgentRunner.__init__ 中创建。SessionManager 每次 send_message 调用 runner_factory 创建新 Runner，所以通常下一轮用户消息使用新注册表，无法通过 agent_result 找到上一轮后台任务。注释写“跨 run 共享”不等于实际生命周期实现了它。

`agent_result` 对已完成 task 检查取消和异常，却没有完整根据 context.status 判断业务失败。有些正常返回但 context 已 failed 的子任务可能被包装成非错误 ToolResult，需要统一终态语义。

## 8 MCP 如何统一为本地工具接口

启动时 Core 创建 McpServerManager；它按配置连接各 server，获取 tools/list，将返回的每个工具定义包装成 McpTool，缓存后让每个 Runner 注册。

命名为 `<server_name>__<tool_name>`，避免不同服务同名工具直接冲突。McpTool 继承 BaseTool，保留外部 inputSchema，调用时转发 tools/call。于是 Loop 不需要知道工具是本地 Python 函数还是外部服务。

通信流程：

```text
stdio 启动子进程 或 TCP 建立连接
→ initialize（代码中请求 protocolVersion=2024-11-05）
→ notifications/initialized
→ tools/list
→ registry 注册包装工具
→ tools/call
→ content 中的 text 拼接为 ToolResult
```

这里只描述该分支实现：stdio 和自定义 TCP JSON 行通信。不能将它概括为支持所有 MCP 传输或所有协议能力；代码没有 HTTP/SSE、完整能力协商、多模态结果处理和工具列表分页。

## 9 MCP 客户端为什么有锁和 stderr 读取任务

`McpClient._call()` 用 Lock 把一次请求到对应响应串行化，避免多个调用同时读同一个 reader。它用递增 ID 关联响应，并把双方 ID 转成字符串比较；通知被跳过，不在这里处理。

stdio 的 stdout 用作协议数据。stderr 必须持续消费，否则子进程输出过多诊断日志时可能因管道缓冲区满而阻塞。客户端创建 `_drain_stderr` 就是为了解决这一问题。

每次读行有 30 秒超时，最大帧 64 MiB。它不是严格的整次调用总超时：如果不断收到无关通知或无效行，每次 readline 都重新计时。外层 invoke_tool 还有自己的超时，应理解多层超时如何叠加。

## 10 MCP 错误语义的遗漏

`_call()` 对 JSON-RPC 外壳的 error 抛 McpToolError，McpTool 将其转成 ToolResult(is_error=True)。但是 `call_tool()` 对 result 内 `isError: true` 未检查，只拼接 text 并返回。

于是一个“RPC 调用成功送达，但工具业务执行失败”的响应，可能被当成成功。这是协议错误与业务错误分层的具体例子。MCP 工具又没有 params_model，执行层还需要落实外部 JSON Schema 校验。

连接中途失败也要考虑清理：start_all 在发现工具失败时记录并跳过，但已打开的 client 不一定进入已管理列表。需在失败路径关闭进程/连接，而不只是正常 stop_all 时清理。

## 11 实验和测试

```powershell
uv run pytest tests/unit/test_skill_loader.py tests/unit/test_agent_profile_loader.py tests/unit/test_spawn_agent_tool.py tests/unit/test_mcp_tool.py -v
```

实验一：创建只含 read_file/list_dir 的 explain Skill，用 FakeProvider 捕获 schema 和实际 prompt，验证参数展开和工具限制。

实验二：后台子 Agent 用 asyncio.Event 控制完成时间；先查仍在运行，再释放 Event，最后查固定结果。进入下一轮新 Runner 再查询同 run_id，验证注册表生命周期问题。

实验三：用假的 MCP 响应分别返回正常 text、JSON-RPC error、result.isError=true，检查三者是否被正确区分。已有 McpTool 测试主要模拟 client 抛异常，不足以覆盖真实响应解析的遗漏。

实验四：父 Skill 只允许 read_file 和 spawn_agent，观察子工具表是否比父级更宽。设计改进前先定义“角色限制”与“会话授权”的交集规则。

## 12 面试问题

**为什么子任务不复制父历史？** 降低无关上下文和成本，让子任务聚焦；代价是父任务必须写清完整目标、输入、约束和期望输出。

**为什么多 Agent 不一定更快？** 额外模型调用、结果整合、共享资源竞争和审批都会增加成本，只有可独立的子任务适合并发。

**MCP 适配器的收益是什么？** 把外部工具转换成统一 BaseTool，使注册、审批、事件与结果回填复用既有流程。

## 13 核心方法逐个精读：Skill 与角色配置

### 13.1 `_parse_skill_file(path)`：文件如何变成 Skill 对象

定位：`core/skills/loader.py`。输入文件路径，输出 Skill 数据对象。先读取文本，默认 name=文件名去后缀、description 为空、allowed_tools 为空、正文为全部文件；如果开头匹配 frontmatter，就把头部与正文拆开，再逐行解析。

`name:` 决定名称；`description:` 支持单行以及 `>`、`|` 后的缩进文本；`allowed_tools:` 这一行本身不做状态切换，随后任何以 `- ` 开头的头部行都可能被收集。因此这是特定格式解析器，不能把完整 YAML 语法随意放进去。

返回对象中的 system_prompt_template 是去掉首尾空白的正文。对于目录式 `name/SKILL.md`，如果不写 name，默认 stem 会是 SKILL；这会影响按对象名称去重的 list_all_skills。学习时建议显式写 name，并让名称与目录一致。

### 13.2 `SkillLoader._search_paths(name)`：先决定查找顺序

它生成六个候选路径：项目扁平文件、项目目录文件、用户扁平文件、用户目录文件、内建扁平文件、内建目录文件。它只构造路径，不读取文件。

把路径生成和解析分开，便于测试覆盖优先级。例如同时创建项目 review.md 与用户 review.md，先检查返回顺序，再测试 resolve 的实际结果。这里项目路径依赖当前进程工作目录，daemon 从不同目录启动可能加载不同 Skill。

### 13.3 `SkillLoader.resolve(name)`：选中第一个存在的候选

逐个检查 _search_paths 的返回值。遇到存在的文件就解析并返回；全部不存在返回 None。若第一个存在的文件解析异常，也直接返回 None，并不继续寻找全局或内建替代文件。

因此“本地坏文件覆盖了好的内建文件”是需要理解的情况。None 同时代表不存在和解析失败，调用方很难提供准确错误提示。改进应把查找结果设计成可区分的状态，并带上来源路径。

### 13.4 `list_all()` 与 `list_all_skills()`：名称列表和对象列表

二者都按内建 → 用户 → 项目枚举，用后来的同名条目覆盖早先条目。为什么与 resolve 顺序相反？resolve 是遇到优先项就停止；列表聚合则用最后一次赋值实现覆盖。

list_all 根据文件名或目录名收集名称；list_all_skills 真正解析文件，按解析出的 `skill.name` 存字典，返回含 description 的对象。文件名与头部 name 不一致时，两种列表可能不一致。后者遇到坏文件会跳过，当前也没有向 UI 展示解析错误。

### 13.5 `render_prompt(skill, arguments)`：简单替换的边界

它返回 `template.replace("$ARGUMENTS", arguments)`，不修改 Skill 对象，也不自动把结果写进 context。输入“请解释 $ARGUMENTS”和“a.py”，输出“请解释 a.py”；模板没有占位符则原样返回。

这是字符串函数，不是执行入口。单测它通过，只能证明替换正确，不能证明模型真的收到替换后的结果。务必继续追到 send_message、Runner 和 Provider.chat 的实参。

### 13.6 `SessionManager.send_message()`：新增 Skill 解析分支

定位：`core/session/manager.py`。正常会话流程见 S4。S7 在用户原文已写入 thread 后，识别 `/名称 参数`，调用 resolve 和 render_prompt，把渲染文本赋给 goal，把模板赋给 system override，再传白名单给 Runner。

这里有三个不同值：用户原文、渲染后的 goal、未渲染的 template。Runner 使用预填历史时，context.messages 里已经有用户原文，不再追加 goal；system override 又保留 template。因此应捕获 Provider 最终收到的 system/messages 进行检查。

单独 `/` 的切分结果为空，访问第一个元素会异常；未知名称返回 None 后继续普通流程。给初学者的调试办法是逐行写出 `content → parts → skill → goal → prefill_messages → system`，不要只看到 goal 被赋值就结束分析。

### 13.7 `AgentRunner._build_registry()` 与 `ExecutionContext.system_prompt`

定位：`core/runner.py`、`core/context.py`。Runner 将非空 whitelist 转为集合，内部 `_ok(name)` 决定是否注册内建工具、notes 工具、子 Agent 工具和 MCP 工具。最终 registry 决定模型可见 schema 和宿主能查到的工具实现。

空列表被转成 None，语义是不限，而不是零权限。若未来需要“这个 Skill 只生成文字”，应明确区分未指定限制和显式空集合。

Context 的 `system_prompt(base)` 方法在 override 非空时使用 override 替代基础提示，之后仍会拼入全局、项目和 notes。override 并不是把所有其他约束来源都删除。检查最终 system 才能知道模型实际看到了什么。

### 13.8 `AgentProfileLoader.load()`、`_search_paths()`、`_parse()`

定位：`core/agents/loader.py`。_search_paths 返回项目、用户、内建三个 TOML 路径；load 选择首个存在文件，解析失败返回 None。代码注释虽写“两级”，实际是三个来源，应以实现为准。

_parse 用 tomllib 读取 `[agent]`，组装 name、description、system_prompt、allowed_tools、model。这里 TOML 由标准解析器处理，但字段内容并没有完整的业务类型验证。比如工具名拼错可能最终表现为没有注册期望工具。

model 只是进入对象字段。沿调用链看 SpawnAgentTool 没有用它创建新 Provider，所以“配置可解析”和“配置已生效”再次需要分开验证。

## 14 核心方法逐个精读：子 Agent

### 14.1 `SpawnAgentTool.__init__()`：保存父环境的引用

定位：`core/subagent/tool.py`。构造器保存 Provider、父 bus、父 run_id、权限管理器、步数上限、后台注册表、运行目录、session_id、depth。它本身不启动任何 Agent。

其中 Provider、PermissionManager、BackgroundTaskRegistry 都是对象引用；传给子 Agent 不等于复制。子消息可以独立，但这些依赖仍可能共享状态。depth 表示当前工具所在的派生层级，用于控制继续派生。

### 14.2 `SpawnAgentTool.invoke(params)`：从工具调用到新 Loop

先用 SpawnAgentParams 校验参数；description 用于展示，prompt 是子任务真正得到的目标，run_in_background 决定等待方式，subagent_type 决定是否加载角色。

随后检查 depth，加载 profile，生成 child_run_id，用 prompt 创建全新的 ExecutionContext，再创建 child_bus 和工具 registry，最后创建 AgentLoop。孩子没有自动拿到父会话 messages，因此 prompt 必须写清输入路径、已知条件、完成标准。

它先向父总线发布 subagent.started，再根据模式执行。前台模式在本方法内部 await 子 Loop，等完成后发布 subagent.finished，再把 context.result 变成 ToolResult；失败时标记 is_error。父 Loop 在等待这次工具调用时不能进入下一次模型调用，但事件循环仍可处理其他任务。

后台模式先 create_task，再 register，立刻返回 run_id。返回“started”证明任务已创建，不证明任务已完成。调用者之后通过 agent_result 查询。

### 14.3 内部 `_bridge(event)`：为什么子事件会出现在父视图

invoke 内定义桥接回调，并订阅 child_bus。子总线每发布事件，回调就 await parent_bus.publish(event)。原事件的 child run_id 被保留，不自动改成父 run_id。

这便于一个父客户端观察子任务进度，但也要求所有消费者按 run 归属分流。如果父 EventWriter 对共享总线不做过滤，子事件就可能写进父专属文件；如果 TUI 只用一个文本块，父子 token 就可能混在一起。

桥接转发属于观察关系，不能把“事件来自同一总线”解释成“属于同一个 run”。S8 会用 parent_run_id 明确表达树关系。

### 14.4 `_build_child_registry()`：能力究竟继承了什么

该方法根据 profile.allowed_tools 生成过滤条件，注册 read_file、bash、write_file、list_dir 和四个任务工具。TaskManager 的存储目录属于 child_run_id，任务列表与父 run 分开。

若当前 depth<1，再尝试注册深度+1 的 spawn_agent 及 agent_result；否则不提供这两个嵌套入口。独立的 invoke 深度检查作为额外保护。深度约束与并发数量约束不同：同一层仍可创建很多任务。

没有自动把父 whitelist 与 profile 取交集，也没有把父 MCP、NoteSaveTool、记忆文件和 Compactor 全部复制进来。学习时可以列一张“共享 / 新建 / 没传入”的表，而不要泛称子 Agent 完全隔离或完全继承。

### 14.5 `_run_background()`：后台协程的真正工作

它打开子运行的 EventWriter，订阅 child_bus，await loop.run(context)，退出文件上下文后发布完成事件。create_task 会安排这段协程执行，返回的 Task 则记录协程是否结束、取消或异常。

这里没有包围整个流程的终态 finally。若等待中被取消，完成事件不一定发出；文件上下文能帮助关闭句柄，却不能自动替你持久化“被取消”状态。之后设计 RunRegistry 时必须统一普通完成、异常和取消路径。

### 14.6 `BackgroundTaskRegistry.__init__/register/get/all`

定位：`core/subagent/registry.py`。内部字典保存 `run_id → (Task, ExecutionContext)`。

register 写入条目；get 返回条目或 None；all 返回所有值组成的列表。注释提到批量取消，但类本身没有 cancel_all 实现，调用者需要遍历 Task 才能取消。它也不会自动清除完成项或落盘。

观察生命周期比记方法更重要：Runner 构造器新建该表，下一轮用户输入通常又新建 Runner，所以跨轮无法保证找到旧后台任务。解决方案要把表提升到会话或 daemon 管理层，并定义会话关闭时如何处理孩子。

### 14.7 `AgentResultTool.__init__()` 与 `invoke()`

构造器保存注册表。invoke 校验 run_id，按以下顺序判断：找不到 → 仍运行 → 已取消 → task.exception 非空 → 返回 context.result。

必须先判断 cancelled 再调用 exception，因为取消状态的 Task 读取异常有特殊行为。另一个层次是业务状态：Task 正常返回只说明 Python 协程没抛出异常，context 仍可能是 failed。当前末分支没有完整检查这个字段，可能把业务失败当普通结果返回。

典型练习：Fake Loop 正常 return，但设置 context.status="failed"，观察 agent_result.is_error。这能证明“程序异常”和“业务失败”不是同一概念。

## 15 核心方法逐个精读：MCP 与配置

### 15.1 `McpClient.__init__()`、`connect_stdio()`、`connect_tcp()`

定位：`core/mcp/client.py`。构造器初始化请求编号、连接字段、Lock 和 stderr task。stdio 连接合并当前环境与指定 env，用 create_subprocess_exec 启动命令，将子进程 stdin/stdout 作为协议通道，并启动 stderr 消费协程；TCP 连接则 open_connection 获得 reader/writer。

两种连接都把最大读取帧设为 64 MiB，然后执行握手。TCP 在这里是本项目自定义的 JSON 行通道，不应据此宣称兼容所有 MCP 服务的网络接口。

create_subprocess_exec 的 command 与 args 分开传递，与 BashTool 执行 shell 字符串不同。配置者仍需要提供本机可执行命令和正确工作环境；连接成功也不代表服务暴露的工具可信或可随意执行。

### 15.2 `_initialize()`：连接建立后确认协议入口

先发 initialize 请求，带代码固定的 protocolVersion、客户端信息和空 capabilities；拿到结果后再发 notifications/initialized。前者有 id 和响应，后者没有响应。

此实现没有完整验证服务端返回的版本与能力。学习时把“完成一次初始化请求”与“支持协议的所有功能”分开。这里没有实现订阅、服务端请求处理或工具列表变化通知的完整流程。

### 15.3 `_call(method, params)`：一个锁保护一问一答

先递增编号并构造 JSON-RPC 请求，再获取 Lock，写一行，循环读取直到找到匹配 id。无法解析的 JSON 行跳过，无 id 通知跳过，不匹配 id 也不会交给另一个 pending Future。

匹配响应有 error 时抛 McpToolError，否则返回 result。ID 比较先转字符串，兼容服务端将数字变为字符串的情况。

这里的锁覆盖“发送到接收响应”的全过程，所以同一个 MCP client 的调用串行执行。S2 的 SocketClient 是一个 reader 根据 id 分发多个 Future；两种设计都能关联响应，但吞吐和复杂度不同。不能看到多个调用协程就声称同一 MCP 连接支持多请求并行。

### 15.4 `_notify()`、`_write_line()`、`_read_line()`

_notify 构造没有 id 的通知，只写不等响应。_write_line 给 JSON 字符串加换行并编码，根据 transport 找 stdin 或 TCP writer，write 后 await drain；没有 writer 时抛 unavailable 错误。

_read_line 要求 reader 存在，每次 readline 等待最多 30 秒；空白行跳过，真正的 `b""` 表示 EOF 并报断连。字节先解码再 strip，返回非空字符串给 _call 解析。

这里的 30 秒是“等下一行”的时间，不是整个 RPC 的固定总期限。持续无关通知可能让总耗时很长。代码捕获某些超限异常，也不能替代对异常封装和内存上限的整体测试。

### 15.5 `list_tools()` 与 `call_tool()`

list_tools 调 tools/list，把返回数组转换为 McpToolDef：name、description、input_schema。没有继续处理分页游标，所以不能假定拿到了服务的所有分页工具。

call_tool 调 tools/call，遍历 content，仅收集 type=text 的 text 并用换行拼接。图片和其他内容没有转换；业务结果中的 isError 没有检查。

要区分两种失败：响应顶层 error 表示 RPC 错误，_call 会抛异常；result.isError=true 表示工具返回业务错误，但当前 call_tool 仍返回普通字符串。这就是下一层 McpTool 无法正确判断该失败的原因。

### 15.6 `_drain_stderr()` 与 `close()`：资源生命周期

_drain_stderr 持续读取子进程 stderr，非空行记 debug；EOF 退出，取消或其他异常结束消费。它避免服务日志填满管道，使原本不涉及协议数据的日志反而卡住进程。

close 先取消并等待 stderr task；stdio 模式 terminate 子进程并最多等待 5 秒，失败后尝试 kill；TCP 模式 close 并 wait_closed。kill 分支之后没有完整再等待退出的处理，连接失败中途的清理也需要调用者保证。

进程、socket、后台 task 都是资源；对每一个创建点都应寻找关闭点及异常路径。只在正常退出时调用 close，不能证明握手失败时不会泄漏。

### 15.7 `McpTool.__init__()` 与 `invoke()`：变成项目统一工具

定位：`core/mcp/tool.py`。构造器保存 client 和原始工具名，对外名称设为 `server__tool`，描述与 schema 来自服务定义。invoke 用原始名和 params 调 client.call_tool，把文本包装成 ToolResult。

连接错误、工具异常及其他异常会转换成 is_error 的 runtime_error。这样 Loop 可以复用统一回填流程；但 S5 对 runtime_error 的通用重试可能重试有副作用的外部工具。适配器不能只统一类型，还应提供“失败是否可重试、结果是否未知”的信息。

该包装器没有 params_model，现执行层不会因此自动完整执行外部 JSON Schema 校验。模型看到 schema 是提示约束，宿主参数校验则是执行约束，不能互相替代。

### 15.8 `McpServerManager.__init__()`、`_connect()`、`start_all()`

定位：`core/mcp/server.py`。构造器建立 clients 字典和 tools 列表。_connect 按 transport 选连接方法，stdio 缺 command 直接报错。

start_all 按配置顺序连接每个服务，发现工具，创建 McpTool 追加到列表，最后把 client 放入按名称索引的字典。某个服务出错会记录日志并继续下一个，因此一个服务不可用不一定导致整个 Core 启动失败。

但“connect 已成功、list_tools 失败”时，client 尚未进入字典，stop_all 无法保证找到它。正确的改进位置是每个服务初始化的异常清理，而不是只在正常关闭循环里加更多判断。

### 15.9 `register_tools()`、`get_tools()`、`stop_all()`

register_tools 遍历缓存工具注册到给定 registry；get_tools 返回工具列表的浅拷贝，Runner 再根据白名单注入自己的 registry。列表复制不会复制其中的 client，因此不同运行可能共享同一个 MCP client 及其串行锁。

stop_all 逐个 close 已登记 client，单个关闭失败只记录，然后清空 clients。当前没有同步清空 tools。若将来支持 manager 原对象重启或热重载，必须避免遗留指向旧连接的工具。

### 15.10 `get_config()` 与 `_apply_toml()`：新增项目配置和 MCP

定位：`core/config.py`。没有显式 KAMA_CONFIG 时按全局、项目配置加载，再应用环境覆盖；显式指定文件时使用该文件路径。标量配置通常后读覆盖先读，但 MCP servers 采用 append，不能把所有字段都概括成同一种覆盖行为。

_apply_toml 校验 mcp 是表、servers 是列表以及名称、transport、command 等字段，再追加 McpServerConfig。同名服务的冲突需要额外处理，否则 clients 按名字覆盖与 tools 继续追加可能不一致。

练习：全局定义一个名为 demo 的服务，项目再定义 demo，观察最终 servers 数量、client 字典和工具列表的行为。配置合并规则是实际功能的一部分，应写进文档并有测试。

### 15.11 `CoreApp.run()`：MCP 如何进入真正运行路径

Core 在启动过程中建立 McpServerManager，await start_all 发现工具，创建 Runner 时注入 manager。Runner 构建 registry 时调用 get_tools 取缓存工具，再按本轮白名单注册，因此“服务已连接”和“本轮可调用该工具”仍然是两个判断。

退出时 Core 调 stop_all，关闭已管理连接。分析启动与退出应成对看：如果某个连接在进入 manager 字典前失败，正常退出循环不会自动拥有它；这一点与前面 start_all 的异常清理问题对应。

### 15.12 SocketServer 与 SocketClient 的收尾变化

S7 将 IPC 流读取上限增大到 64 MiB，并在服务端跟踪活跃 writer，stop 时关闭这些连接，改善只关闭监听器而已连接客户端仍存活的问题。

增大上限解决部分大消息传输限制，也扩大单帧内存占用边界；它不等于有了按字节计量的背压。关闭 writer 与取消所有 handler task、子 Agent task 也是不同层次的清理，仍需分别检查所有者。

## 16 核心方法逐个精读：TUI 补全与最终消息

本节组件都位于 `tui/app.py`，没有独立的 widgets 目录。

### 16.1 `SlashCompleteWidget` 的方法组

构造器保存完整候选 `(name, description)` 列表、过滤结果和选中索引。set_query 用小写子串匹配名称，再把索引限制在合法范围并重绘；不是总从第一条重新选择。

move_up/move_down 在有候选时循环移动；has_selection 判断是否存在候选；select_current 发出 Selected 消息，把选中名称交给 App。on_mount 调 _redraw，_redraw 将候选及选中标记绘制出来，没候选则显示相应提示。

它只是选择组件，不执行 Skill、不访问模型。这样的拆分让“键盘如何选中”与“选中后执行什么业务”可以分别测试。

### 16.2 `ChatTextArea.on_text_area_changed()` 与 `_on_key()`

文本以 `/` 开头且还没空格时，输入框发布 SlashChanged(query)；否则发布关闭候选的状态。输入 `/rev` 时应该匹配命令名，输入 `/review a.py` 时已经开始填参数，不应继续把整段当命令名搜索。

_on_key 根据补全是否存在处理上下键、Tab、Enter、Esc；有可选项时 Enter 先选择补全，普通提交时才发 Submitted，带修饰键的换行另行处理。事件停止传播是为了避免同一个按键既选中候选又意外发送消息。

### 16.3 App 的 `_build_slash_items()` 与两个消息处理器

_build_slash_items 放入内建 compact 命令，再读取 list_all_skills 获取名称与描述。`on_chat_text_area_slash_changed` 创建、更新或移除弹层；`on_slash_complete_widget_selected` 将输入框填为 `/名称 ` 并关闭弹层，方便继续输入参数。

补全来源在 TUI 进程读取，实际 Skill 在 Core 进程解析。如果双方 cwd 或文件视图不同，候选存在不代表后端同样能 resolve。未来可让 Core 返回可用命令列表，减少双端重复解释配置。

### 16.4 Provider 与 Loop 对 `thinking` block 的保留

S7 Provider 从最终响应提取 thinking 文本及 signature，放入 LlmResponse.thinking_blocks；Loop 组装 assistant content 时保留这些块，以满足后续请求的消息协议要求。

这里学习的是协议数据透传：不要自行改写签名，也不要把所有未识别 block 都随意丢弃。它并不意味着本项目能检查模型完整的内部推理过程；文档与简历应描述为响应块兼容和消息结构维护。

## 17 把三种扩展串起来做一次纸面推演

输入 `/explain src/example.py`，按顺序记录 Skill 文件来源、原始输入、模板、渲染文本、最终 system、最终 messages、registry 工具名。即使你暂时不用真实模型，也可以用 FakeProvider 捕获这些值。

然后构造一个 spawn_agent 调用，分别列出父子双方的 run_id、messages、bus、TaskManager 目录、Provider 对象、PermissionManager 对象。你应得到“部分新建、部分共享”的结论，而不是笼统说全部隔离。

最后让子任务调用一个 MCP 工具：先检查子 registry 是否真的注册了它。当前默认子工具表没有自动继承 MCP，因此调用链在工具发现层就可能断掉，不能因为父模型能用就推断孩子也能用。

完成本章的标准是：能够解释每条能力是由哪一层授权、在哪个 registry 注册、由哪个对象执行、如何回填结果，以及对象在下一轮和重启后是否还存在。

## 18 本阶段可改进的地方

| 改进 | 具体方向 | 验收 |
| --- | --- | --- |
| Skill 参数可靠传递 | 将渲染结果明确传到实际 system/messages，处理单独 `/` | 捕获请求不含未替换占位符，空输入不崩溃 |
| 能力限制传递 | 子工具集与父级有效能力取交集，区分空与未指定 | 子 Agent 不能通过重新加载角色扩大权限 |
| 后台任务生命周期 | registry 提升到 session/daemon；终态持久化；取消和配额 | 下一轮能查结果，父级退出策略明确，数量有上限 |
| 模型策略接线 | 根据 profile.model 构建可测试 Provider 工厂 | 角色选择确实改变调用目标，并统计成本 |
| MCP 错误处理 | 解析 isError，校验 schema，失败清理连接 | 业务失败不会伪装成功，断线不留子进程 |
| 可读 TUI 并行流 | token 按 run_id/attempt 分桶，后台结果有入口 | 多个子 Agent 输出不混写、失败可以定位 |

不要为了简历增加更多子 Agent 名称。优先修能力边界、生命周期和错误语义，再用固定任务集证明并发在何种条件下有效。
