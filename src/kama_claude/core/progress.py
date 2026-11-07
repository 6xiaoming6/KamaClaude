from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from kama_claude.core.tools.base import BaseTool, FileTool, ToolResult
from kama_claude.core.workspace import WorkspacePathError

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.checkpoint import RunCheckpoint

_READ_TOOLS = frozenset({
    "read_file", "list_dir", "find_files", "search_text",
    "get_file_outline", "search_symbol", "read_symbol",
})
_MAX_PERIOD = 3
_WARN_REPEATS = 3


def validate_progress_limit(value: int) -> int:
    if type(value) is not int or (value != 0 and not 4 <= value <= 20):
        raise ValueError("no_progress_limit must be 0 (disabled) or an integer from 4 to 20")
    return value


class ReadObservation(BaseModel):
    action: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProgressState(BaseModel):
    # 只保存有限指纹，不复制工具全文；仍随唯一 Run 一起提交。
    recent: list[ReadObservation] = Field(default_factory=list, max_length=60)
    warned_pattern: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warned_step: int | None = Field(default=None, ge=0)
    stopped: bool = False


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProgressGuard:
    # 仅识别明确的只读重复模式；不猜测任务语义，也不调用额外模型。
    def __init__(self, run: RunCheckpoint) -> None:
        self.run = run

    def _reset(self) -> None:
        self.run.progress = ProgressState()

    def observe(
        self, tool: BaseTool | None, params: dict[str, Any], result: ToolResult,
    ) -> tuple[str, bool]:
        run = self.run
        if not run.no_progress_limit:
            return "", False
        if (
            tool is None or tool.name not in _READ_TOOLS or result.outcome_unknown
            or result.error_type == "permission_denied"
        ):
            # 写入、命令、审批拒绝及子任务查询等都打断观察窗口，避免误判轮询。
            self._reset()
            return "", False

        normalized = dict(params)
        if tool.params_model is not None:
            try:
                normalized = tool.params_model.model_validate(params).model_dump(mode="json")
            except ValidationError:
                pass  # 参数错误也可以形成重复，但不能掩盖原错误。
        if isinstance(tool, FileTool):
            try:
                normalized["path"] = os.path.normcase(str(tool.path_access(normalized).target))
            except WorkspacePathError:
                self._reset()
                return "", False
        sample = ReadObservation(
            action=_digest([tool.name, normalized]),
            output=_digest([result.content, result.is_error, result.error_type]),
        )
        state = run.progress
        if any(old.action == sample.action and old.output != sample.output for old in state.recent):
            self._reset()  # 相同查询出现新结果，视为环境变化，不沿用旧的重复证据。
            state = run.progress
        state.recent.append(sample)
        state.recent = state.recent[-_MAX_PERIOD * run.no_progress_limit:]
        pattern, period, repeats = self._pattern(state.recent)
        if pattern is None:
            state.warned_pattern = state.warned_step = None
            return "", False
        if state.warned_pattern != pattern:
            state.warned_pattern, state.warned_step = pattern, run.step
            return (
                f"[No progress warning: the same read/search pattern (period {period}) "
                f"repeated {repeats} times with unchanged results. Use the evidence already "
                "available, change the approach, or explain the blocker and finish honestly.]",
                False,
            )
        # 同一回复可能包含一整批重复工具，先让模型看到提醒，下一 Step 仍重复才停止。
        if repeats >= run.no_progress_limit and run.step > (state.warned_step or 0):
            state.stopped = True
            return "[No progress limit reached: read/search pattern continued after warning.]", True
        return "", False

    @staticmethod
    def _pattern(recent: list[ReadObservation]) -> tuple[str | None, int, int]:
        for period in range(1, _MAX_PERIOD + 1):
            if len(recent) < period * _WARN_REPEATS:
                continue
            block = recent[-period:]
            repeats = 1
            while len(recent) >= period * (repeats + 1):
                end = len(recent) - period * repeats
                if recent[end - period:end] != block:
                    break
                repeats += 1
            if repeats >= _WARN_REPEATS:
                # AB 与 BA 是同一个循环，不能因窗口移动而反复获得一次新提醒。
                tokens = [f"{item.action}:{item.output}" for item in block]
                canonical = min(tokens[offset:] + tokens[:offset] for offset in range(period))
                return _digest(canonical), period, repeats
        return None, 0, 0
