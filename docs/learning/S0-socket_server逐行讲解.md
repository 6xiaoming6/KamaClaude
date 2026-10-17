# S0：socket_server.py 逐行讲解

这份说明对应当前 stage/s0 的 [socket_server.py](../../src/kama_claude/core/transport/socket_server.py)。行号按本次阅读时的 141 行版本标注；以后代码变化时，以方法名定位。

阅读目标：能够解释服务怎样开始监听、连接回调何时执行、请求如何被解析和分发、响应怎样返回，以及谁负责关闭资源。

## 1 先明确它的职责

SocketServer 是 Core 的通信入口，负责 TCP 监听、按行读取消息、校验请求外壳、根据 method 查找处理函数、包装响应并发送。

ping 的业务结果由 CoreApp._ping_handler 生成。SocketServer 不计算版本、运行时长，也不直接解释用户目标或调用模型。这样新增命令时可以复用同一套通信和错误处理。

CoreApp 中的用法只有几个关键动作：

```python
server = SocketServer(config.host, config.port)
server.register("core.ping", self._ping_handler)
addr = await server.start()
```

构造对象 → 注册业务方法 → 建立监听。start 返回后，CoreApp 继续等待退出通知；客户端连接由 asyncio 调度的连接回调处理。

## 2 分清三个层次：服务器、连接、请求

| 层次 | 对应对象或方法 | 创建或执行次数 |
| --- | --- | --- |
| 监听服务器 | self._server | 正常启动时建立一个监听服务 |
| 一条客户端连接 | reader、writer、_handle_connection | 每个新连接执行一次连接回调 |
| 一条业务请求 | _handle_line | 同一连接可以多次处理请求 |

**连接和请求不能混为一谈。** 客户端 A 建一个连接，连续发两个 JSON 行：连接回调执行一次，_handle_line 执行两次。客户端 B 再连接，则有另一组 reader、writer 和另一条连接处理协程。

```text
SocketServer
  handlers：所有连接共享的方法注册表
  listener：host:port 上的监听服务
  ├─ 连接 A：reader A / writer A → 请求 A1、A2……
  └─ 连接 B：reader B / writer B → 请求 B1、B2……
```

S0 的 CLI 每次 ping 新建连接、只发一次请求，但服务端读取循环支持一条连接持续接收多行。

## 3 文件开头的类型和常量

### 3.1 CommandHandler 是什么

第 23 行：

```python
type CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]
```

这是 Python 3.12 的类型别名写法。它描述“命令处理函数应该长什么样”：可以调用；接收一个字典参数；调用后得到可等待的结果；等待完成后返回业务数据。

逐段翻译：

- Callable：这个对象可以像函数一样调用。
- 第一个方括号中的 dict：调用时传入一个字典。
- Awaitable：调用者要用 await 等待结果。
- Any：类型检查不限定具体结果类型，不代表可以自动序列化任何 Python 对象。

符合这个形状的例子是 CoreApp._ping_handler(params)。这里只规定接口，真正输入校验要靠后面的 model_validate 或 handler 自己的业务模型。

### 3.2 _MAX_LINE_BYTES 是什么

第 25 行把服务端 StreamReader 的 limit 设为 1×1024×1024，即 1 MiB。它用于限制按行读取的缓冲边界，防止无约束的大行输入。

设上限与正确处理超限异常是两件事。当前 readline 超限的异常接线仍有待完善，见第 12 节。

### 3.3 单下划线表示什么

_host、_handlers、_handle_line 这类命名表示内部实现约定。它不是语言强制的访问限制。其他模块技术上仍可访问，但正常使用应走 register/start/stop 等公开接口。

## 4 __init__：建立对象状态，第 29 行

```python
self._host = host
self._port = port
self._handlers: dict[str, CommandHandler] = {}
self._server: asyncio.AbstractServer | None = None
```

host/port 指定监听地址，例如 127.0.0.1 和 7437。_handlers 保存方法到函数的映射；_server 保存真正的监听服务器句柄。

初始化时 _server=None，因为还没执行 start。`SocketServer(...)` 只是在内存里建对象，不会自动绑定端口，也不会开始处理连接。

这里没有返回业务结果，效果是把对象准备成一个可被后续方法使用的状态。

## 5 register：把方法名关联到函数，第 36 行

```python
self._handlers[method] = handler
```

调用：

```python
server.register("core.ping", self._ping_handler)
```

之后字典可以理解为：

```text
"core.ping" → CoreApp 对象绑定的 _ping_handler 方法
```

注意传的是 self._ping_handler，没有括号。没有括号表示把函数本身保存下来；收到请求后，SocketServer 才会调用它。

如果此时就写成 self._ping_handler(...)，会变成提前调用，而不是登记未来可调用的方法。对 async 方法，还会先得到协程对象，不能拿它替代可重复调用的 handler。

同名注册会覆盖旧函数；register 当前不检查名称重复。它只改内存字典，不访问网络。

## 6 start：探测占用并启动监听，第 40 行

### 6.1 为什么先调用 open_connection

```python
_r, w = await asyncio.open_connection(self._host, self._port)
```

这里暂时扮演客户端，试着连接目标地址，检查是否已有监听者。

连接成功时，关闭探测连接、等待关闭，然后 SystemExit 提示地址已被占用。_r 是没使用的 reader，用下划线前缀表达“这里不用这个返回值”。

连接被拒绝或出现相应 OSError 时，进入 except，继续尝试真正启动服务。这是探测逻辑，最终能不能监听仍由后面的 bind 决定。

SystemExit 不属于 OSError，因此不会被这个 except 吞掉；连接成功后的退出分支不会继续往下创建第二个监听服务。

### 6.2 真正的服务器在哪里创建

第 49 行：

```python
self._server = await asyncio.start_server(
    self._handle_connection,
    host=self._host,
    port=self._port,
    limit=_MAX_LINE_BYTES,
)
```

start_server 建立 TCP 监听。第一个参数是“每次出现新连接时执行的回调”，这里传入 _handle_connection 方法。

此刻是在注册回调，并非手动调用 _handle_connection。以后某个客户端建立连接，asyncio 会把该连接的 reader、writer 交给它；因为回调是 async 方法，asyncio 会将其安排为任务。

返回的 AbstractServer 句柄保存在 self._server，供 stop 关闭。start 方法自己返回 host:port 字符串，给 Core 日志展示。

**常见疑问：start 返回了，服务器为什么还在运行？** 监听服务已注册进事件循环，后续连接由回调处理，不依赖 start 方法一直停在原地。Core 的事件循环继续运行，所以回调可以继续推进。

### 6.3 探测的两个边界

连接成功只能证明有进程监听，不证明对方一定是兼容的 Core。探测与实际 bind 之间也有间隔，可能被其他进程抢先占用端口。因此探测是提示机制，不是原子锁，也不是完整健康检查。

## 7 _handle_connection：管理一条连接，第 65 行

输入 reader 和 writer，分别表示这条连接的读取端与写入端。它们不是所有客户端共用的一对对象。

第一步取 peername，记录对端地址用于日志。第二步 await _read_loop，持续处理这条连接。第三步在 finally 中关闭 writer。

```python
try:
    await self._read_loop(reader, writer)
finally:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except TimeoutError:
        pass
```

_read_loop 正常返回、读取途中抛错或任务被取消，都会尝试进入 finally。资源清理放这里，可以覆盖多条退出路径。

close 发起流关闭；wait_closed 等待关闭完成。这里最多等 1 秒，超时就不再等，避免清理阶段无限悬挂。它没有吞掉所有可能的异常，只明确忽略了等待关闭的 TimeoutError。

**为什么关闭 writer 就能关闭连接？** asyncio 的 StreamWriter 关联底层网络 transport/socket，close 操作会向底层发起关闭，不是简单删除一个变量。reader/writer 是同一条连接的两个操作接口。

## 8 _read_loop：从连接里取出一条条消息，第 83 行

主体可以概括为：

```python
while True:
    line = await reader.readline()
    if not line:
        return
    await self._handle_line(line, writer)
```

### 8.1 readline 为什么合适

TCP 是字节流，无法把一次 write 自动还原成一次业务请求。S0 约定每条 JSON 以换行结尾，所以用 readline 取帧。

reader.readline 返回 bytes。比如：

```python
b'{"jsonrpc":"2.0","id":"cli-1","method":"core.ping","params":{}}\n'
```

这是一段字节，不是 JsonRpcRequest，也不是已经解析好的字典。下一层 _handle_line 才处理语义。

### 8.2 if not line 判断什么

返回 b"" 表示连接到达 EOF 且没有剩余数据，读取循环结束。它与只发一个空行不同：b"\n" 不是空字节，会交给 JSON 解析，并产生解析错误。

如果连接保持开启且没有完整换行，readline 可以继续等待；若中途 EOF，也可能返回未带换行的残余字节。当前实现没有额外强制检查每次非空返回都必须有换行。

### 8.3 同一连接请求为什么按顺序处理

因为这里是 await _handle_line，上一条处理和发送没有结束，就不会执行下一次 readline。

这不妨碍其他连接在等待 I/O 时被调度，但它意味着同连接内没有并行请求分发。后续长任务与审批消息可能需要改变这个设计。

## 9 _handle_line：一条请求的处理中心，第 101 行

这个方法最值得逐行读。按“解析 → 校验 → 查找 → 执行 → 包装”五步理解。

### 9.1 第一步：字节转成 Python 数据

```python
raw: Any = json.loads(line)
```

json.loads 可以接收 JSON 字节数据，解析出 Python 值。合法输入可能是字典，也可能是数组、数字等，因此 raw 使用 Any，而没有直接假定它就是请求字典。

若语法错误，捕获 JSONDecodeError，构造 -32700 Parse Error 并发送，然后 return。本方法不继续查 handler。

此时未获得可信请求 ID，错误外壳用 id=None，序列化后是 null。

### 9.2 第二步：Python 数据转成请求模型

```python
req = JsonRpcRequest.model_validate(raw)
```

这一步检查能否符合请求外壳：id、method 等字段是否存在、值的结构和类型是否可接受，jsonrpc 是否能符合 2.0 的模型定义。

语法合法不代表业务结构合法。例如 raw=123 可以是合法 JSON，但不能成为这里的请求对象。外壳校验失败时返回 -32600 Invalid Request，并停止处理。

注意模型有默认值，不等于“严格拒绝所有规范缺省或未知字段”。例如 jsonrpc 默认 2.0，缺少它时模型可补默认值。

### 9.3 第三步：按 method 查找 handler

```python
handler = self._handlers.get(req.method)
```

请求 method=core.ping 时，拿到先前 register 的函数。未知 method 返回 None，再构造 -32601 Method Not Found。

这时请求外壳已经校验成功，所以错误响应可以带回 req.id。客户端靠它知道这条错误对应哪次请求。

### 9.4 第四步：调用真正的业务方法

```python
result = await handler(req.params)
```

传给 handler 的是业务参数字典，不是整个 JsonRpcRequest。handler 在 CoreApp 中生成 PongResult，SocketServer 等待它完成。

如果 handler 抛 ValidationError，当前分支返回 -32600，消息为 Invalid params。这里理想上应使用已定义的 -32602，是当前的协议一致性缺口。

其他普通 Exception 会记录 traceback 到服务端日志，再给客户端返回 -32603 Internal Error。客户端拿到的是结构化错误，不能依赖服务端 Python 堆栈作为协议。

这段 try 只包 handler 执行；并不自动捕获整个文件任何位置发生的异常，例如后面的结果序列化和网络发送失败仍可能向上传播。

### 9.5 第五步：统一包装成功响应

```python
result_data: Any = result.model_dump() if isinstance(result, BaseModel) else result
await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
```

如果 handler 返回 Pydantic 对象，先转普通数据；若返回普通字典等值则直接使用。然后把业务结果放进 result，连同原请求 id 一起组成成功响应。

Any 表示未限制结果静态类型，不保证任意对象都能被 JSON 序列化。新增 handler 仍应返回约定的、可序列化结果。

**为什么不让 handler 自己发送？** 将业务计算与协议外壳分开，所有命令都复用请求 ID、错误处理、编码和换行规则。这样扩展更清楚，也更容易分别测试。

## 10 _send：把结果交回连接，第 139 行

```python
writer.write(msg.model_dump_json().encode() + b"\n")
await writer.drain()
```

从内到外读：model_dump_json 得到 JSON 字符串；encode 默认转为 UTF-8 字节；加 b"\n" 作为消息结束标记；write 提交给网络传输；drain 等待到适合继续写的缓冲状态。

例如发送成功响应：

```json
{"jsonrpc":"2.0","id":"cli-1","result":{"server_version":"0.0.1","uptime_ms":100,"received_at":"2026-10-17T08:00:00+00:00"}}
```

真正发送的字节在此 JSON 后还有换行。错误响应也调用 _send，因此成功和失败使用相同编码与分帧方式。

drain 不表示客户端已经显示结果，也不表示业务效果已被对端确认。缓冲压力小时 drain 可以立即返回，不应认为每个 await 都一定切换到其他协程。

## 11 stop：关闭监听服务器，第 58 行

如果尚未启动，_server=None，直接 return；否则调用 AbstractServer.close，并最多等 2 秒 wait_closed。

这里 self._server.close 与 writer.close 不是同一个关闭对象：

| 操作 | 主要对象与作用 |
| --- | --- |
| self._server.close() | 关闭监听服务，停止接受新连接 |
| writer.close() | 关闭某一条已建立连接的网络流 |

stop 本身没有保存并遍历所有客户端 writer，也没有完整的任务取消与排空策略。不要把“监听端口可以重新绑定”直接等同于“所有业务任务都按产品策略结束”。

当前 CoreApp 在退出 finally 中 await stop，Windows 最小兼容修改就接在这里；平台信号处理属于 app.py，socket_server.py 主要处理网络。

## 12 当前实现值得记住的边界

1. **端口探测不等于健康检查。** 任意监听程序都可能让探测成功。
2. **同连接内顺序执行。** 长 handler 会推迟该连接下一条消息的处理。
3. **业务参数校验尚未完整接入。** ping handler 直接读取字典，没有使用 PingCommand.model_validate。
4. **参数错误码未区分正确。** handler 的 ValidationError 当前返回 -32600，而非 -32602。
5. **超限异常需核实。** Python 3.12 readline 某些超限会抛 ValueError，当前只捕获 LimitOverrunError，不能认定所有大帧都已妥善处理。
6. **读行等待没有显式超时。** 一直连接但不发完整数据的客户端可占着协程等待。
7. **关闭监听不等于完整排空。** 所有连接、请求任务的生命周期还需更完整管理。
8. **协议形态受限。** 当前入口要求字符串 id、字典 params，不实现完整批量调用和通知处理。

这些是理解与后续改进的入口，本次讲解没有修改 socket_server.py。

## 13 用一次实际请求串起来

假设收到请求：

```json
{"jsonrpc":"2.0","id":"request-7","method":"core.ping","params":{"client":"study"}}
```

先由 start 提前建立监听。客户端连接时 _handle_connection 获得 reader/writer，_read_loop 取一行字节；_handle_line 依次得到 raw 字典、req 模型和 _ping_handler 函数；await 业务函数得到 PongResult；包装成功外壳后 _send 发出字节。响应 id 仍为 request-7。

客户端关闭后，readline 返回空字节，读取循环 return，连接回调进入 finally 关闭流。主 Core 仍等待其他客户端，直到用户通知退出，再调用 stop 关闭监听。

对应调用关系：

```text
CoreApp.run
  ├─ SocketServer(...)
  ├─ register("core.ping", handler)
  └─ start
       └─ asyncio 在每个新连接到来时调度 _handle_connection
            └─ _read_loop：每次循环处理一条请求
                 └─ _handle_line
                      ├─ json.loads
                      ├─ JsonRpcRequest.model_validate
                      ├─ handlers.get(method)
                      ├─ await handler(params)
                      └─ _send：编码、加换行、写入、drain

连接结束：_handle_connection 的 finally → writer.close
Core 退出：CoreApp 的 finally → SocketServer.stop
```

## 14 读完后用这六题检查

1. 为什么 register 传 self._ping_handler 而不加括号？
2. 一个连接连续发送两个 JSON 行，_handle_connection 和 _handle_line 各执行几次？
3. 合法 JSON 数字 123 到哪一层才会被拒绝？
4. method 不存在时，为什么可以返回原请求 ID？
5. start 已经返回，什么机制让服务继续接收客户端？
6. 为什么 stop 和关闭某个 writer 不能互相替代？

参考答案依次是：保存可调用对象；一次与两次；请求模型校验层；外壳已校验并取得 id；事件循环管理监听与连接回调；二者分别拥有监听资源和连接资源。

## 15 本文件可改进的地方

先选一个小问题形成修复闭环：接入 PingCommand 校验并修正参数错误码，或用超长行实验补齐异常清理。验收覆盖合法输入、目标故障和另一个正常连接，确保处理问题输入不会破坏正常服务。

后续有长任务时再设计请求任务管理、同连接并发和读写超时；扩展能力时保留上述生命周期与分帧回归。更多问题见 [S0 自测与面试标准答案](S0-核心自测与面试标准答案.md)。
