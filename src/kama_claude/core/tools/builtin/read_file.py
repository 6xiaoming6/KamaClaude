from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import Field

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_OUTPUT_BYTES, ReadBudget, text_lines
from kama_claude.core.tools.images import is_image, read_image


class ReadFileParams(ToolParams):
    path: str = Field(description="UTF-8 text file or local PNG/JPEG/WebP/GIF image.")
    start_line: int = Field(default=1, ge=1, description="First line to read, numbered from 1.")
    max_lines: int = Field(default=200, ge=1, le=2000, description="Maximum source lines per page.")


class ReadFileTool(FileTool):
    retry_safe = True
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read a UTF-8 text file with line numbers and pagination. Relative paths use the fixed "
        "workspace; native absolute paths are accepted, with approval outside the workspace. "
        "Returns at most 50 KiB; lines over 2000 characters are shortened. "
        "Use next_start_line to continue text. PNG/JPEG/WebP/GIF images are attached as real "
        "visual input for vision models; do not parse pixels using shell scripts. Images are "
        "limited to 5 MiB/20 MP, resized to at most 1536 px per side; GIF uses the first frame. "
        "Line pagination applies to text only. Other binary files are rejected."
    )
    input_schema = ReadFileParams.model_json_schema()

    def __init__(self, workspace: Path | None = None, *, image_support: bool = True) -> None:
        super().__init__(workspace)
        self._image_support = image_support

    # 在线程中有界读取，避免扫描大文件时阻塞 Core 的 TCP 事件循环
    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult:
        parsed = ReadFileParams.model_validate(params)
        return await asyncio.to_thread(self._read, path, parsed)

    def _read(self, path: Path, params: ReadFileParams) -> ToolResult:
        if is_image(path):
            if not self._image_support:
                raise ToolError("current model does not support image input", "invalid_operation")
            if params.start_line != 1 or params.max_lines != 200:
                raise ToolError("start_line/max_lines pagination is only supported for text")
            return read_image(path)
        return self._read_page(path, params)

    # 只保存当前页，返回下一行位置；超长行的剩余部分排空但不累积
    def _read_page(self, path: Path, params: ReadFileParams) -> ToolResult:
        budget = ReadBudget()
        overhead = len(str(path).encode("utf-8")) + 1024
        if overhead >= MAX_OUTPUT_BYTES:
            raise ToolError("file path exceeds the output metadata budget", "resource_limit")
        rows: list[str] = []
        size = 0
        next_line: int | None = None
        shortened = False
        for line in text_lines(path, budget, line_limit=2000):
            if line.number < params.start_line:
                continue
            row = f"{line.number} | {line.text}" + (" … [line truncated]" if line.shortened else "")
            row_size = len(row.encode("utf-8")) + 1
            if len(rows) >= params.max_lines or size + row_size > MAX_OUTPUT_BYTES - overhead:
                next_line = line.number
                break
            rows.append(row)
            size += row_size
            shortened = shortened or line.shortened
        end_line = params.start_line + len(rows) - 1
        if budget.reason:
            next_line = params.start_line + len(rows)
        header = (
            f"File: {path}\nLines: {params.start_line}-{end_line if rows else 'none'}\n"
            f"next_start_line: {next_line if next_line is not None else 'none'}"
        )
        if next_line is not None or budget.reason or shortened:
            header += f"\n[truncated: {budget.reason or 'page_or_line_limit'}]"
        return ToolResult(content=header + ("\n" + "\n".join(rows) if rows else "\n(empty range)"))
