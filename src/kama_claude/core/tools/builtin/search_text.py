from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any, Self

import regex
from pydantic import Field, model_validator

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_OUTPUT_BYTES, ReadBudget, text_lines
from kama_claude.core.tools.traversal import DirectoryScanner, matches_glob
from kama_claude.core.workspace import WorkspacePathError


class SearchTextParams(ToolParams):
    pattern: str = Field(min_length=1, max_length=512)
    path: str = "."
    include: str = Field(default="**/*", min_length=1, max_length=512)
    literal: bool = True
    case_sensitive: bool = True
    context_lines: int = Field(default=1, ge=0, le=3)
    limit: int = Field(default=100, ge=1, le=200)
    include_ignored: bool = False

    @model_validator(mode="after")
    # 非法正则在审批和执行前报参数错误，逐行搜索不接收多行模式
    def validate_pattern(self) -> Self:
        if "\n" in self.pattern or "\r" in self.pattern:
            raise ValueError("search_text searches one line at a time")
        if not self.literal:
            try:
                regex.compile(self.pattern)
            except regex.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
        return self


class SearchTextTool(FileTool):
    name = "search_text"
    retry_safe = True
    description = (
        "Search UTF-8 file contents and return JSON paths, line numbers and short context. "
        "Literal matching is default; literal=false enables a regex with a per-match timeout. "
        "path may be a directory or a single file. Relative paths use the workspace and external "
        "paths require approval. Skips binary/invalid text, links and ignored entries; reports "
        "partial scans. Regexes do not span lines. Output is limited to 50 KiB."
    )
    params_model = SearchTextParams
    input_schema = SearchTextParams.model_json_schema()

    # 在线程内进行有界只读搜索，取消调用不会继续产生文件副作用
    async def _invoke_path(self, params: dict[str, object], root: Path) -> ToolResult:
        return await asyncio.to_thread(self._search, root, SearchTextParams.model_validate(params))

    # 遍历与文本读取共享预算，超限返回已找到结果及明确的截断原因
    def _search(self, root: Path, params: SearchTextParams) -> ToolResult:
        if not root.exists():
            raise FileNotFoundError(f"path not found: {root}")
        base = root if root.is_dir() else root.parent
        budget = ReadBudget()
        scanner = DirectoryScanner(base, self.paths.root, params.include_ignored, budget)
        files = scanner.files() if root.is_dir() else iter([root])
        expression = regex.compile(
            regex.escape(params.pattern) if params.literal else params.pattern,
            0 if params.case_sensitive else regex.IGNORECASE,
        )
        matches: list[dict[str, Any]] = []
        skipped_files = 0
        unread_files = 0
        skipped_lines = 0
        reason = ""
        for path in files:
            if not matches_glob(path.relative_to(base).as_posix(), params.include):
                continue
            before: deque[dict[str, object]] = deque(maxlen=params.context_lines)
            pending: list[dict[str, Any]] = []
            try:
                for line in text_lines(path, budget):
                    context = {"line": line.number, "text": line.text[:400]}
                    for previous in pending:
                        previous["after"].append(context)
                    pending = [
                        item for item in pending if len(item["after"]) < params.context_lines
                    ]
                    if line.shortened:
                        skipped_lines += 1
                        before.append(context)
                        continue
                    found = expression.search(line.text, timeout=0.05)
                    if found is not None:
                        if len(matches) >= params.limit:
                            reason = "result_limit"
                            break
                        start = max(0, found.start() - 100)
                        record: dict[str, Any] = {
                            "path": path.relative_to(base).as_posix(), "line": line.number,
                            "text": line.text[start:start + 400],
                            "before": list(before), "after": [],
                        }
                        matches.append(record)
                        if params.context_lines:
                            pending.append(record)
                    before.append(context)
            except TimeoutError:
                reason = "regex_timeout"
            except WorkspacePathError:
                raise
            except (UnicodeError, ToolError):
                skipped_files += 1
            except (PermissionError, FileNotFoundError):
                skipped_files += 1
                unread_files += 1
            if reason or not budget.check():
                break
        reason = (
            reason or budget.reason or scanner.reason or ("line_limit" if skipped_lines else "")
            or ("unreadable_entries" if unread_files or scanner.skipped else "")
        )
        result = {
            "root": str(root), "matches": matches,
            "truncated": bool(reason or scanner.skipped), "reason": reason,
            "skipped_files": skipped_files, "skipped_lines": skipped_lines,
            "unreadable_files": unread_files,
            "skipped_directories": scanner.skipped,
        }
        # 后续上下文也计入输出上限，不能因命中很多行而撑爆模型输入
        while matches and len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
            matches.pop()
            result["truncated"], result["reason"] = True, "output_limit"
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ToolError("result metadata exceeds output limit", "resource_limit")
        return ToolResult(content=json.dumps(result, ensure_ascii=False))
