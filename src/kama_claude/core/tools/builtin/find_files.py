from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import Field, field_validator

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_OUTPUT_BYTES, ReadBudget
from kama_claude.core.tools.traversal import DirectoryScanner, matches_glob


class FindFilesParams(ToolParams):
    pattern: str = Field(min_length=1, max_length=512, description="File glob, e.g. **/*.py.")
    path: str = "."
    limit: int = Field(default=100, ge=1, le=200)
    include_ignored: bool = False

    @field_validator("pattern")
    # 模式只选择目录内文件，不承担路径解析或目录跳转
    def validate_pattern(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("use path for the search root and a relative glob pattern")
        return normalized


class FindFilesTool(FileTool):
    name = "find_files"
    retry_safe = True
    description = (
        "Find file paths by case-sensitive glob, e.g. **/*test*.py. A filename pattern searches "
        "all levels. Relative path uses the workspace; external paths require approval. "
        "Skips ignored/generated entries and links by default. Returns JSON paths, truncation "
        "and scan information. Use include_ignored=true to inspect ignored files."
    )
    params_model = FindFilesParams
    input_schema = FindFilesParams.model_json_schema()

    # 扫描只读目录在线程内执行，超时和数量上限由扫描器控制
    async def _invoke_path(self, params: dict[str, object], root: Path) -> ToolResult:
        return await asyncio.to_thread(self._find, root, FindFilesParams.model_validate(params))

    # 收集至上限后一条，才能区分恰好达到上限与真实截断
    def _find(self, root: Path, params: FindFilesParams) -> ToolResult:
        if not root.exists():
            raise FileNotFoundError(f"directory not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        budget = ReadBudget()
        scanner = DirectoryScanner(root, self.paths.root, params.include_ignored, budget)
        found: list[str] = []
        reason = ""
        for path in scanner.files():
            relative = path.relative_to(root).as_posix()
            if matches_glob(relative, params.pattern):
                if len(found) >= params.limit:
                    reason = "result_limit"
                    break
                found.append(relative)
        reason = reason or budget.reason or scanner.reason
        if not reason and scanner.skipped:
            reason = "unreadable_directories"
        result = {
            "root": str(root), "files": found, "truncated": bool(reason or scanner.skipped),
            "reason": reason, "skipped_directories": scanner.skipped,
        }
        while found and len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
            found.pop()
            result["truncated"], result["reason"] = True, "output_limit"
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
            raise ToolError("result metadata exceeds output limit", "resource_limit")
        return ToolResult(content=json.dumps(result, ensure_ascii=False))
