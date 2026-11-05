from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint

HANDOFF_INSTRUCTIONS = """

请用简洁 Markdown 交接成果，依次使用以下标题：
## 结论
## 证据
## 已完成
## 验证
## 遗留事项
证据注明文件路径及必要行号；验证只描述实际执行的检查。
未验证、失败或结果未知的事项要明确说明，不要推断为成功。
只返回必要结论，不粘贴大段原始日志；详细资料注明文件位置。
"""
_SUMMARY_LIMIT = 4_000


# 保留原文并使用足够长的代码围栏，模型内容不能伪装成后端报告章节。
def _fenced(text: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


# 从真实事件记录收集当前 Run 的命令结果，不解析模型文本或猜测退出码。
def _command_records(path: Path, run_id: str) -> list[str]:
    pending: dict[str, str] = {}
    records: list[str] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                event: dict[str, Any] = json.loads(line)
                if event.get("run_id") != run_id or event.get("tool_name") != "bash":
                    continue
                uid = str(event.get("tool_use_id", ""))
                kind = event.get("type")
                if kind == "tool.call_started":
                    pending[uid] = str(event.get("params", {}).get("command", ""))
                elif kind == "tool.call_finished":
                    records.append(f"[工具返回成功] {pending.pop(uid, '(未记录命令)')}")
                elif kind == "tool.call_failed" and not event.get("retrying"):
                    records.append(
                        f"[工具返回错误: {event.get('error_class', 'unknown')}] "
                        f"{pending.pop(uid, '(未记录命令)')}"
                    )
    except (OSError, ValueError, TypeError, AttributeError):
        records.append("事件记录缺失或不完整，不能据此确认全部执行结果。")
    records.extend(f"[未收到最终结果] {command}" for command in pending.values())
    return records or ["当前 Run 没有已记录的命令调用。"]


# 同目录临时文件完整落盘后再替换报告，失败时不留下半份 report.md。
def write_report(run: RunCheckpoint, run_path: Path) -> Path:
    path = run_path / "report.md"
    events = run_path / "events.jsonl"
    text = (
        f"# 子 Agent 成果交接\n\n## 后端记录\n\n"
        f"- Run ID：{run.run_id}\n- 状态：{run.status}\n- 原因：{run.reason or '无'}\n"
        f"- 实际 Token：{run.actual_tokens}\n- 估算占用：{run.estimated_tokens}\n"
        f"- 用量完整：{run.usage_complete}\n- 执行秒数：{run.elapsed_seconds:.3f}\n"
        f"- 事件日志：{events}\n\n"
        "## 命令工具记录\n\n只记录工具返回状态，不推算退出码或测试通过数量。\n\n"
        f"{_fenced(chr(10).join(_command_records(events, run.run_id)))}\n\n"
        "## 模型回答原文\n\n以下内容来自模型，不是后端独立验证的结论。\n\n"
        f"{_fenced(run.result)}\n"
    )
    temporary: Path | None = None
    try:
        run_path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=run_path,
            prefix=".report-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return path
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# 父任务只接收有限回答和完整报告位置，后台缓存也不保留超长模型回答。
def summarize(text: str) -> tuple[str, bool]:
    return text[:_SUMMARY_LIMIT], len(text) > _SUMMARY_LIMIT
