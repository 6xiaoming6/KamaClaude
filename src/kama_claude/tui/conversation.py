from __future__ import annotations

import time
from typing import Any

from rich.markup import escape
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from kama_claude.tui.formatting import _preview, render_context_bar
from kama_claude.tui.widgets.conversation import LLMStreamBlock, ToolCallBlock


class ConversationView(VerticalScroll):
    def __init__(self, *, id: str = "log-view") -> None:
        super().__init__(id=id)
        self.reset()

    # 展示状态只由视图持有，连接与会话调度由 App 负责。
    def reset(self) -> None:
        self.current_llm: LLMStreamBlock | None = None
        self.current_llm_run_id: str | None = None
        self.pending_tools: dict[tuple[str, str], ToolCallBlock] = {}
        self.completed_tools: set[tuple[str, str]] = set()
        self.completed_runs: set[str] = set()
        self.final_replies: set[str] = set()
        self.finish_calls: set[tuple[str, str]] = set()
        self.streamed_text: dict[str, str] = {}
        self.subagent_runs: dict[str, str] = {}
        self.subagent_start_times: dict[str, float] = {}
        self.last_context_pct = 0.0

    def append(self, widget: Widget) -> None:
        self.mount(widget)
        self.scroll_end(animate=False)

    def break_stream(self) -> None:
        if self.current_llm is not None:
            self.current_llm.finalize_markdown()
        self.current_llm = None

    # 显示完整原文；Run ID 区分不同任务复用的模型工具 ID。
    def render_history(self, messages: list[dict[str, Any]]) -> None:
        legacy_batch: dict[str, tuple[str, str]] = {}
        for index, message in enumerate(messages):
            content = message.get("content")
            if isinstance(content, str):
                if message["role"] == "user":
                    self.append(Static("> " + content, markup=False, classes="user-turn"))
                elif content:
                    block = LLMStreamBlock()
                    block.append_token(content)
                    block.finalize_markdown()
                    self.append(block)
                continue
            if message["role"] == "assistant":
                legacy_batch = {}
                self.streamed_text[str(message.get("run_id", ""))] = ""
            run_id = str(message.get("run_id", ""))
            # 旧记录没有归属信息时，只生成本地显示键，不猜测真实 Run。
            display_id = run_id or f"legacy:{message.get('id', index)}"
            for item in content or []:
                if item.get("type") == "text":
                    self.streamed_text[display_id] = (
                        self.streamed_text.get(display_id, "") + str(item.get("text", ""))
                    )
                    block = LLMStreamBlock()
                    block.append_token(item.get("text", ""))
                    block.finalize_markdown()
                    self.append(block)
                elif item.get("type") == "tool_use":
                    key = (display_id, item["id"])
                    legacy_batch[item["id"]] = key
                    if item["name"] == "finish":
                        self.finish_calls.add(key)
                        continue
                    tool = ToolCallBlock(item["name"], item.get("input", {}))
                    self.pending_tools[key] = tool
                    self.append(tool)
                elif item.get("type") == "tool_result":
                    key = (
                        (run_id, item["tool_use_id"])
                        if run_id
                        else legacy_batch.get(
                            item["tool_use_id"], (display_id, item["tool_use_id"])
                        )
                    )
                    if key in self.finish_calls:
                        self.finish_calls.remove(key)
                        text = self._tool_result_text(item.get("content", ""))
                        if item.get("is_error"):
                            self.append(Static(
                                "结束提交未通过：" + text,
                                markup=False, classes="log-line",
                            ))
                        else:
                            self._show_final_reply(key[0], text)
                        self.completed_tools.add(key)
                        continue
                    result_block = self.pending_tools.pop(key, None)
                    if result_block is not None:
                        result_block.set_result(
                            self._tool_result_text(item.get("content", "")),
                            0,
                            is_error=bool(item.get("is_error")),
                        )
                        self.completed_tools.add(key)

    # 实时终态与历史重放共用，参数、工具输出和重连事件不再各显示一遍答案。
    def _show_final_reply(self, run_id: str, text: str) -> None:
        if not text or run_id in self.final_replies:
            return
        self.final_replies.add(run_id)
        self.break_stream()
        previous = self.streamed_text.get(run_id, "").rstrip()
        if previous and previous.endswith(text.rstrip()):
            return
        block = LLMStreamBlock()
        block.append_token(text)
        block.finalize_markdown()
        self.append(block)

    @staticmethod
    def _tool_result_text(content: Any) -> str:
        if isinstance(content, list):
            return "\n".join(str(block.get("text", "")) for block in content
                             if block.get("type") == "text")
        return str(content)

    def handle_event(self, event: dict[str, Any]) -> None:
        prefix = str(event.get("type", "")).split(".", 1)[0]
        if prefix == "llm":
            self._handle_llm(event)
        elif prefix == "run":
            self._handle_run(event)
        elif prefix == "skill":
            self._handle_skill(event)
        elif prefix == "subagent":
            self._handle_subagent(event)
        elif prefix == "step":
            self._handle_step(event)
        elif prefix == "tool":
            self._handle_tool(event)
        elif prefix == "context":
            self._handle_context(event)
        elif prefix == "log":
            self._handle_log(event)

    def _handle_llm(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            if self.current_llm_run_id != event.get("run_id"):
                self.break_stream()
                self.current_llm_run_id = event.get("run_id")
            token = event.get("token", "")
            run_id = str(event.get("run_id", ""))
            self.streamed_text[run_id] = self.streamed_text.get(run_id, "") + str(token)
            if self.current_llm is None:
                llm_block = LLMStreamBlock()
                self.append(llm_block)
                self.current_llm = llm_block
            self.current_llm.append_token(token)
            return
        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self.subagent_runs:
                return
            pct = float(event.get("context_pct") or 0.0)
            self.last_context_pct = pct
            ctx_bar = render_context_bar(pct)
            total_input = event.get("total_input_tokens")
            if total_input is None:
                total_input = event.get("input_tokens")
            self.append(
                Static(
                    f"[dim]  tokens  "
                    f"in={total_input} "
                    f"out={event.get('output_tokens')} "
                    f"cache={event.get('cache_read_input_tokens')}[/dim]"
                    f"  {ctx_bar}",
                    classes="usage",
                )
            )

    def _handle_run(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self.append(
                Static(
                    f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                    classes="run-header",
                )
            )
        elif t == "run.finished":
            run_id = str(event.get("run_id", ""))
            if run_id in self.completed_runs:
                return
            self.completed_runs.add(run_id)
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._show_final_reply(run_id, str(event.get("result") or ""))
                label = "本轮已提交" if event.get("result") else "completed"
                self.append(
                    Static(
                        f"[bold green]✓ {label}[/bold green]  [dim]{steps} steps[/dim]",
                        classes="run-ok",
                    )
                )
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self.append(
                    Static(
                        f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                        classes="run-err",
                    )
                )
        elif t == "run.budget":
            if str(event.get("run_id", "")) in self.subagent_runs:
                return
            method = "Usage+增量" if event.get("method") == "usage_anchor" else "UTF-8字节"
            estimate = event.get("estimated_input_tokens", 0)
            actual = event.get("actual_tokens", 0)
            uncertain = event.get("estimated_tokens", 0)
            budget_elapsed = float(event.get("elapsed_seconds", 0))
            reason = str(event.get("reason") or "")
            if reason == "model_retry":
                self.break_stream()
                self.append(Static("上段模型输出未完成，正在重新请求。", markup=False))
            budget_text = (
                f"预算：{method}输入估算 {estimate}"
                + (
                    f" / 压缩阈值 {event.get('trigger_tokens')}"
                    if event.get("trigger_tokens")
                    else ""
                )
                + f"；累计已知 {actual}"
                f" + 估算占用 {uncertain} tokens；执行 {budget_elapsed:.1f}s"
                + (f"；提示：{reason}" if reason else "")
            )
            budget_id = "budget-" + str(event.get("run_id", ""))
            matches = [widget for widget in self.query(Static) if widget.id == budget_id]
            if matches:
                matches[0].update(budget_text)
            else:
                self.append(
                    Static(
                        budget_text,
                        id=budget_id,
                        markup=False,
                        classes="usage",
                    )
                )

    def _handle_skill(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self.append(
                Static(
                    f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                    classes="log-line",
                )
            )

    def _handle_subagent(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self.subagent_runs[run_id] = description
            self.subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self.append(
                Static(
                    f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]"
                    f"  [dim]{short_id}[/dim]",
                    classes="log-line",
                )
            )
        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self.subagent_runs.pop(run_id, event.get("description", ""))
            start = self.subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if "actual_tokens" in event:
                desc_part += (
                    f"  [dim]已知 {event.get('actual_tokens', 0)}"
                    f" + 估算 {event.get('estimated_tokens', 0)} tokens[/dim]"
                )
            if event.get("reason"):
                desc_part += f"  [dim]{escape(str(event['reason']))}[/dim]"
            if status == "success":
                self.append(
                    Static(
                        f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                        classes="log-line",
                    )
                )
            else:
                self.append(
                    Static(
                        f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                        classes="log-line",
                    )
                )

    def _handle_step(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "step.started":
            run_id = event.get("run_id", "")
            self.streamed_text[str(run_id)] = ""
            if run_id in self.subagent_runs:
                return
            step = event.get("step", "")
            self.append(
                Static(
                    f"[dim]step {step}[/dim]",
                    classes="step-divider",
                )
            )

    def _handle_tool(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if event.get("tool_name") == "finish":
            if t == "tool.call_failed" and not event.get("retrying"):
                self.append(Static(
                    "结束提交未通过：" + str(event.get("error_message", "")),
                    markup=False, classes="log-line",
                ))
            return
        if t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            key = (str(event.get("run_id", "")), tool_use_id)
            if key in self.pending_tools or key in self.completed_tools:
                return
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self.subagent_runs:
                tc_block.styles.padding = (0, 2, 0, 6)
            self.pending_tools[key] = tc_block
            self.append(tc_block)
        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            key = (str(event.get("run_id", "")), tool_use_id)
            if key in self.completed_tools:
                return
            self.completed_tools.add(key)
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if key in self.pending_tools:
                tc_done = self.pending_tools.pop(key)
                tc_done.set_result(output, elapsed_ms)
        elif t == "tool.call_failed":
            if event.get("retrying"):
                return
            tool_use_id = str(event.get("tool_use_id", ""))
            key = (str(event.get("run_id", "")), tool_use_id)
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            self.completed_tools.add(key)
            error_msg = str(event.get("error_message") or "")
            if key in self.pending_tools:
                tc_done = self.pending_tools.pop(key)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

    def _handle_context(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "context.compaction_started":
            self.append(
                Static(
                    "[dim]⚡ 正在摘要旧历史，保留近期原文…[/dim]",
                    classes="log-line",
                )
            )
        elif t == "context.compaction_failed":
            reasons = {
                "no_old_history": "没有需要压缩的旧历史",
                "incomplete_tool_batch": "工具消息尚未完整配对",
                "recent_context_too_large": "必须保留的近期原文过大",
                "summary_input_too_large": "完整摘要请求超出输入预算",
                "summary_request_failed": "摘要请求失败",
                "summary_incomplete": "摘要输出未完整结束",
                "empty_summary": "摘要为空",
                "summary_too_large": "摘要超过长度上限",
                "insufficient_reduction": "压缩后没有明显缩小",
                "context_budget_exceeded": "整理后仍超出输入预算",
                "required_context_too_large": "固定提示与必须保留的近期原文已超预算",
                "provider_unavailable": "需要摘要，但模型配置不可用",
                "below_threshold": "空间充足，无需继续压缩",
                "auto_disabled": "自动摘要已关闭",
            }
            reason = reasons.get(str(event.get("reason")), str(event.get("reason", "")))
            retained = (
                "候选未采用"
                if event.get("applied_stages")
                else "未替换工作历史"
            )
            usage = "已尝试 LLM 摘要" if event.get("llm_used") else "未调用 LLM"
            self.append(
                Static(
                    f"⚡ 上下文整理：{reason}；{retained}；{usage}。",
                    classes="log-line",
                    markup=False,
                )
            )
        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            remaining = event.get("remaining_tokens", summary)
            stage_names = {
                "llm_summary": "LLM 摘要",
            }
            stages = " → ".join(stage_names.get(s, s) for s in event.get("applied_stages", []))
            label = (
                "已生成 LLM 摘要" if event.get("llm_used", True) else "程序整理完成（未调用 LLM）"
            )
            self.append(
                Static(
                    f"⚡ {label}  input≈{orig} → {remaining} tokens; summary≈{summary}"
                    + f"  保留 {event.get('preserved_messages', 0)} 条消息"
                    + (f"  {stages}" if stages else ""),
                    classes="log-line",
                    markup=False,
                )
            )

    def _handle_log(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self.append(
                Static(
                    f"[{color}]{level}[/{color}]  "
                    f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                    classes="log-line",
                )
            )
