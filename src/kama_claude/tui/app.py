from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import Button, Label, Static

from kama_claude.core.config import KamaConfig
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.transport.socket_client import IpcError, SocketClient
from kama_claude.tui.conversation import ConversationView
from kama_claude.tui.formatting import _preview
from kama_claude.tui.memory import MemoryScreen
from kama_claude.tui.recovery import RecoveryActions, SessionPicker
from kama_claude.tui.widgets.input import ChatTextArea, SlashCompleteWidget
from kama_claude.tui.widgets.permissions import PermissionBlock, PermissionSelect

log = logging.getLogger(__name__)


class KamaTuiApp(App[None]):
    """KamaClaude TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "KamaClaude"
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+o", "open_sessions", "历史会话", priority=True),
        Binding("ctrl+w", "close_session", "关闭会话", priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        "[bold cyan]██╗  ██╗ █████╗ ███╗   ███╗ █████╗  ██████╗"
        "██╗      █████╗ ██╗   ██╗██████╗ ███████╗[/bold cyan]\n"
        "[bold cyan]██║ ██╔╝██╔══██╗████╗ ████║██╔══██╗██╔════╝"
        "██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝[/bold cyan]\n"
        "[bold cyan]█████╔╝ ███████║██╔████╔██║███████║██║     "
        "██║     ███████║██║   ██║██║  ██║█████╗  [/bold cyan]\n"
        "[bold cyan]██╔═██╗ ██╔══██║██║╚██╔╝██║██╔══██║██║     "
        "██║     ██╔══██║██║   ██║██║  ██║██╔══╝  [/bold cyan]\n"
        "[bold cyan]██║  ██╗██║  ██║██║ ╚═╝ ██║██║  ██║╚██████╗"
        "███████╗██║  ██║╚██████╔╝██████╔╝███████╗[/bold cyan]\n"
        "[bold cyan]╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝"
        "╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  "
        "Ctrl+Q 退出 · Ctrl+O 会话 · Ctrl+W 关闭会话[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._view = ConversationView()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._opening = False
        self._open_lock = asyncio.Lock()
        self._recovering = False
        self._event_buffer: list[dict[str, Any]] = []
        self._last_event_seq = 0
        self._snapshot_revision = 0
        self._slash_items: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Label("[bold]KamaClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield self._view
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._view.append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("compact", "compress context window"),
            ("memory", "查看 Markdown 记忆与 Run 注入快照"),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        # 模态查看窗口打开后，忽略旧聊天输入框迟到的补全事件
        if self._prompt() is None or isinstance(self.screen, MemoryScreen):
            return
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = select._key_map.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(select._choices)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(select._choices)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(select._choices[select._cursor][0])
        except Exception:
            pass

    # 退出仅断开界面，保留可重新打开的会话
    async def action_quit(self) -> None:
        self.exit()

    # 打开历史选择器，不取消当前 Core 中的运行
    def action_open_sessions(self) -> None:
        if self._client is not None and not self._opening:
            self.run_worker(self._pick_session(), name="session_picker", exclusive=False)

    # 主动关闭会话与退出界面是两个不同动作
    def action_close_session(self) -> None:
        self.run_worker(self._close_session(), name="close_session", exclusive=False)

    # 向 Core 发送显式关闭请求
    async def _close_session(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            await self._client.send_command("session.close", {"session_id": self._session_id})
        except (IpcError, RuntimeError, OSError) as exc:
            self._view.append(Static(f"[red]close error: {exc}[/red]"))

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        if content == "/memory":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None:
                self.run_worker(self._do_memory(), name="memory", exclusive=False)
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._view.append(
                Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line")
            )
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = "agent is working..."
        self._view.append(Static(f"[bold]>[/bold] {content}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 只读查看记忆，RPC 不产生用户消息或 Agent Run
    async def _do_memory(self) -> None:
        client, sid = self._client, self._session_id
        if client is None or sid is None:
            return
        try:
            data = await client.send_command("memory.inspect", {"session_id": sid, "limit": 50})
            if self._client is client and self._session_id == sid:
                await self.push_screen(MemoryScreen(client, sid, data))
        except (IpcError, RuntimeError, OSError) as exc:
            self._view.append(
                Static("记忆读取失败：" + str(exc), markup=False, classes="run-err")
            )

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            # 进度与结果统一由会话事件展示，RPC 响应不再重复追加横幅
            await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._view.append(
                Static(f"[red]compact error: {e}[/red]", classes="log-line")
            )

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        client, sid = self._client, self._session_id
        if client is None:
            return
        try:
            await client.send_command(
                "session.send_message",
                {"session_id": sid, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as exc:
            if self._client is not client or self._session_id != sid:
                return
            self._view.append(Static(f"[red]send error: {exc}[/red]", classes="log-line"))
            if isinstance(exc, ConnectionError):
                self._update_header("disconnected")
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
            else:
                await self._refresh_session()

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    # self.post_message(self.Decided(...))后，框架会自动调用on_permission_select_decided
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        request_id = msg.request_id
        decision = msg.decision
        log.info("permission decided request_id=%s decision=%s", request_id, decision)
        try:
            # 后端确认有效后才移除控件；非法或失败的响应不能伪装成已批准。
            if self._client is None:
                return
            try:
                await self._client.send_command(
                    "permission.respond",
                    {
                        "request_id": request_id,
                        "decision": decision,
                        "session_id": msg.widget.session_id,
                        "run_id": msg.widget.run_id,
                    },
                )
            except (IpcError, RuntimeError, OSError) as exc:
                self._view.append(
                    Static(f"审批未提交：{exc}", classes="log-line", markup=False)
                )
                return
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(request_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed request_id=%s", request_id)

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
            "interrupted": "yellow",
            "closed": "dim",
        }.get(state, "dim")
        header.update(
            f"[bold]KamaClaude[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}  [{color}]{state}[/{color}]"
        )

    # 选择历史或新建会话，仅用户明确操作才恢复中断任务，选择后并打开会话
    async def _pick_session(self) -> None:
        if self._client is None:
            return
        try:
            # 向 Core 发送请求获取会话列表
            result = await self._client.send_command("session.list", {})
            sessions = result["sessions"]
            # 显示选择框，等待用户选择
            selected: str | None
            # 没有历史会话默认创建新会话
            if not sessions:
                selected = "__new__"
            else:
                # 有历史会话，可以选择打开历史会话或者创建新的会话
                selected = await self.push_screen_wait(SessionPicker(sessions))
            # 创建新会话
            if selected == "__new__":
                created = await self._client.send_command("session.create", {"mode": "chat"})
                selected = str(created["session_id"])
            # 打开选中的会话
            if selected is not None:
                await self._open_session(selected)
        except (IpcError, RuntimeError, OSError) as exc:
            self._view.append(Static(f"[red]open error: {exc}[/red]"))

    # 先缓冲推送，再用权威快照重建显示，最后应用水位之后的事件
    async def _open_session(self, sid: str) -> None:
        async with self._open_lock:
            await self._load_session(sid)

    # 在已持有打开锁时加载会话的历史快照并刷新组件，防止多个恢复通知交叉覆盖
    async def _load_session(self, sid: str) -> None:
        if self._client is None:
            return
        previous = self._session_id
        # 加载期间先缓冲事件，避免历史快照与实时更新交叉覆盖
        self._opening = True
        self._event_buffer = []
        self._session_id = sid
        try:
            data = await self._client.send_command("session.open", {"session_id": sid})
            view = self._view
            await view.remove_children()
            view.reset()
            self._pending_permission_blocks.clear()
            for select in list(self.query(PermissionSelect)):
                select.remove()
            for control in list(self.query(RecoveryActions)):
                control.remove()
            self._snapshot_revision = int(data["revision"])
            # 水位过滤旧事件，完成 ID 再防止迟到通知重复显示
            self._last_event_seq = int(data.get("event_seq", 0))
            # 后端返回 thread 完整原文；压缩后的工作上下文只供模型使用
            self._view.render_history(data["messages"])
            # 拿到该会话检查点的运行状态
            run = data.get("run")
            if run:
                self._view.completed_tools.update(
                    {
                        (run["run_id"], tool["id"])
                        for tool in run["tools"]
                        if tool["status"] == "completed"
                    }
                )
                # 状态为interrupted就需要用户决定是否从中断恢复运行
                if run["status"] == "interrupted":
                    self._view.append(RecoveryActions(self._snapshot_revision, run))
                # 状态为成功或者失败都说明上次的运行是完整的
                elif run["status"] in ("success", "failed"):
                    self._view.completed_runs.add(run["run_id"])
                    self._view.append(
                        Static(
                            f"run {run['run_id']} — {run['status']} ({run['step']} steps)",
                            classes="run-ok" if run["status"] == "success" else "run-err",
                        )
                    )
            # 拿到整个该会话的整体状态
            status = data["session"]["status"]
            self._busy = bool(data["running"]) or status == "interrupted"
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = self._busy or status == "closed"
                prompt.read_only = False
                prompt.border_title = (
                    "任务中断：请确认继续或终止"
                    if status == "interrupted"
                    else "session closed — Ctrl+O 选择会话"
                    if status == "closed"
                    else "agent is working..."
                    if self._busy
                    else "输入消息"
                )
                if not prompt.disabled:
                    prompt.focus()
            self._update_header(
                "interrupted"
                if status == "interrupted"
                else "closed"
                if status == "closed"
                else "running"
                if self._busy
                else "ready"
            )
            for permission in data.get("pending_permissions", []):
                self._handle_event_inner(permission)
        except BaseException:
            self._session_id = previous
            raise
        finally:
            self._opening = False
            buffered, self._event_buffer = self._event_buffer, []
            for event in buffered:
                self._handle_event(event)

    # 恢复按钮只提交明确决定，不在重连后自动启动执行
    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "recovery-continue": "continue",
            "recovery-retry": "retry_tool",
            "recovery-abort": "abort",
        }
        action = actions.get(event.button.id or "")
        if action is not None and not self._recovering:
            self._recovering = True
            self.run_worker(self._resume_session(action), name="resume", exclusive=False)

    # 使用用户看到的修订提交决定，冲突后重新读取权威状态
    async def _resume_session(self, action: str) -> None:
        try:
            if self._client is None or self._session_id is None:
                return
            await self._client.send_command(
                "session.resume",
                {
                    "session_id": self._session_id,
                    "expected_revision": self._snapshot_revision,
                    "action": action,
                },
            )
            await self._open_session(self._session_id)
        except (IpcError, RuntimeError, OSError) as exc:
            if self._client is not None and self._session_id is not None:
                try:
                    await self._open_session(self._session_id)
                except (IpcError, RuntimeError, OSError):
                    pass
            self._view.append(Static(f"[red]resume error: {exc}[/red]"))
        finally:
            self._recovering = False

    # 中断通知后刷新快照，按钮与未知工具信息来自持久化状态
    async def _refresh_session(self) -> None:
        if self._session_id is not None:
            try:
                await self._open_session(self._session_id)
            except (IpcError, RuntimeError, OSError):
                log.exception("session refresh failed")

    # 保留会话身份进行重连，Core 中的任务不随 TCP 连接取消
    async def _socket_loop(self) -> None:
        # 外层循环只在连接结束后创建新客户端，正常连接停在 receiver 等待处
        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue
            self._client = client

            # 初始化快照期间暂存实时事件
            async def on_event(event: dict[str, Any]) -> None:
                if self._opening:
                    self._event_buffer.append(event)
                else:
                    self._handle_event(event)

            client.on_event(on_event)
            # 客户端创建事件循环，开始接收服务端发来的内容
            receiver = asyncio.create_task(client.run_event_loop())
            try:
                if self._replay_run_id is not None:
                    await client.send_command(
                        "event.subscribe",
                        {
                            "topics": ["*"],
                            "scope": f"run:{self._replay_run_id}",
                            "replay_from_run": self._replay_run_id,
                        },
                    )
                    self._update_header("replay")
                # 如果是重连，还会保存上一次的_session_id，直接打开会话
                elif self._session_id is not None:
                    try:
                        await self._open_session(self._session_id)
                    except IpcError as exc:
                        self._view.append(Static(f"[red]open error: {exc}[/red]"))
                        self._session_id = None
                        await self._pick_session()
                # 如果是重启客户端后第一次连接，需要选择打开历史会话还是创建新会话
                else:
                    await self._pick_session()
                # 打开或创建会话后，就挂起持续接收服务端的消息，直到连接中断
                # 此时前端tui还可以响应其他的事情
                await receiver
            except (IpcError, RuntimeError, OSError):
                log.exception("socket session failed")
            finally:
                if not receiver.done():
                    receiver.cancel()
                try:
                    await receiver
                except asyncio.CancelledError:
                    pass
                self._client = None
                self._view.break_stream()
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.border_title = "disconnected, retrying..."
                await client.close()
            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        if self._replay_run_id is None and self._session_id is not None:
            if event.get("session_id") != self._session_id:
                return
        seq = event.get("event_seq")
        if isinstance(seq, int):
            if seq <= self._last_event_seq:
                return
            self._last_event_seq = seq
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))


    # App 处理会话和审批操作；普通展示事件交给对话视图。
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type", ""))
        if kind != "llm.token":
            self._view.break_stream()
        if kind in {
            "session.waiting_for_input",
            "session.closed",
            "run.interrupted",
            "run.resumed",
        }:
            self._handle_session_event(event)
        elif kind.startswith("permission."):
            self._handle_permission_event(event)
        else:
            self._view.handle_event(event)

    def _handle_session_event(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")
        elif t == "run.interrupted":
            self._view.append(
                Static(
                    "任务已中断：" + str(event.get("reason", "interrupted")),
                    markup=False,
                    classes="run-err",
                )
            )
            self._busy = True
            self._update_header("interrupted")
            self.run_worker(self._refresh_session(), name="refresh", exclusive=False)
        elif t == "run.resumed":
            self._busy = True
            self._update_header("running")
        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("closed")

    def _handle_permission_event(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "permission.requested":
            request_id = str(event.get("request_id", ""))
            if not request_id or request_id in self._pending_permission_blocks:
                return
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name,
                tool_use_id,
                _focused_repr,
            )
            path_details = ""
            if event.get("resolved_path"):
                params = event.get("params") or {}
                path_details = (
                    f"原始路径：{params.get('path', '.')}\n"
                    f"实际路径：{event['resolved_path']}\n工作区：{event.get('workspace', '')}"
                )
            if tool_name == "bash":
                params = event.get("params") or {}
                path_details = (
                    f"完整命令：\n{params.get('command', '')}\n"
                    f"工作目录：{event.get('workspace', '')}\n"
                    "执行方式：本机 shell（未隔离文件和网络访问）"
                )
            if event.get("outside_workspace"):
                params = event.get("params") or {}
                operation = {
                    "read_file": "读取",
                    "write_file": "写入",
                    "list_dir": "列举目录",
                    "find_files": "查找文件",
                    "search_text": "搜索内容",
                    "edit_file": "局部编辑",
                }
                path_details = (
                    f"工作区外访问 · {operation.get(tool_name, tool_name)}（仅本次授权）\n"
                    f"原始路径：{params.get('path', '.')}\n"
                    f"实际路径：{event.get('resolved_path', '')}\n"
                    f"工作区：{event.get('workspace', '')}"
                )
            if tool_name == "edit_file":
                params = event.get("params") or {}
                old = _preview("\n".join(str(params.get("old_text", "")).splitlines()[:4]), 240)
                new = _preview("\n".join(str(params.get("new_text", "")).splitlines()[:4]), 240)
                path_details += f"\n编辑片段预览（最多四行）：\n旧：\n{old}\n新：\n{new}"
            path_details += (
                f"\n审批原因：{event.get('reason', '')}"
                f"\n授权范围：{event.get('approval_scope', '仅本次操作')}"
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview, path_details)
            self._pending_permission_blocks[request_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._view.append(perm_block)
            select = PermissionSelect(
                request_id,
                event.get("allowed_decisions"),
                session_id=str(event.get("session_id", "")),
                run_id=str(event.get("run_id", "")),
                tool_name=tool_name,
            )
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )
        elif t in {"permission.denied", "permission.granted"}:
            # 处理超时或断连等非用户交互触发的 deny
            # 用户主动 deny 已由 on_permission_select_decided 处理
            request_id = str(event.get("request_id", ""))
            decision = str(event.get("decision", "denied"))
            if request_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(request_id)
                perm_block._resolve(decision)
                try:
                    for select in self.query(PermissionSelect):
                        if select._request_id == request_id:
                            select.remove()
                except Exception:
                    pass
                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()


# TUI 入口：读取配置并启动 KamaTuiApp
def run(config: KamaConfig, replay_run_id: str | None = None) -> None:
    app = KamaTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()
