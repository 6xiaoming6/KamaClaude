from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import Field

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import ReadBudget
from kama_claude.core.tools.output import fits_result, json_result
from kama_claude.core.tools.symbols import SourceFile, Symbol, parse_source


class ReadSymbolParams(ToolParams):
    path: str = Field(min_length=1, description="Concrete Python, JS/JSX or TS/TSX file.")
    name: str = Field(min_length=1, max_length=256, description="Exact simple or qualified name.")
    max_lines: int = Field(default=200, ge=1, le=2000, description="Maximum source lines per page.")
    start_line: int | None = Field(
        default=None, ge=1,
        description="Absolute source line: definition start to disambiguate, or next_start_line.",
    )
    expected_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
        description="file_hash from the previous read; a changed file returns conflict.",
    )


class ReadSymbolTool(FileTool):
    name = "read_symbol"
    retry_safe = True
    params_model = ReadSymbolParams
    input_schema = ReadSymbolParams.model_json_schema()
    description = (
        "Read the current source of one definition in a concrete Python, JS/JSX or TS/TSX file. "
        "Returns JSON symbol range, file_hash, exact source page and next_start_line. "
        "Use qualified names from search_symbol/get_file_outline. Ambiguous names return "
        "candidates; pass the definition start_line to select one. Continue with next_start_line "
        "and expected_hash to detect edits between pages. Source lines are never silently cut. "
        "Syntax errors are reported; use read_file for unsupported or oversized source. "
        "Relative paths use the workspace; external paths follow file approvals."
    )

    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult:
        return await asyncio.to_thread(self._read, path, ReadSymbolParams.model_validate(params))

    def _read(self, path: Path, params: ReadSymbolParams) -> ToolResult:
        source = parse_source(path, ReadBudget())
        if params.expected_hash is not None and params.expected_hash != source.digest:
            raise ToolError("file changed; search/read the definition again", "conflict")
        candidates = [symbol for symbol in source.symbols if symbol.matches(params.name)]
        if not candidates:
            if source.reason:
                raise ToolError(f"definition not located in partial parse ({source.reason}); "
                                "inspect the source with read_file")
            raise ToolError(f"symbol not found: {params.name}", "not_found")
        if len(candidates) > 1 and params.start_line is not None:
            # 定义首行优先；续读行必须仅属于一个同名候选，不能随便挑选。
            starts = [item for item in candidates if item.start_line == params.start_line]
            candidates = starts or [item for item in candidates
                                    if item.start_line <= params.start_line <= item.end_line]
        if not candidates:
            raise ToolError("start_line is outside all matching definitions")
        if len(candidates) > 1:
            return json_result({
                "root": str(path.parent), "status": "ambiguous", "file_hash": source.digest,
                "symbols": [item.record(path.name) for item in candidates],
                "truncated": bool(source.reason), "reason": source.reason,
                "hint": "Use a qualified name and definition start_line to select one candidate.",
            }, "symbols")
        return self._page(path, source, candidates[0], params)

    def _page(
        self, path: Path, source: SourceFile, symbol: Symbol, params: ReadSymbolParams,
    ) -> ToolResult:
        start = params.start_line if params.start_line is not None else symbol.start_line
        if not symbol.start_line <= start <= symbol.end_line:
            raise ToolError("start_line is outside the selected definition")
        result: dict[str, object] = {
            "root": str(path.parent), "status": "ok", "file_hash": source.digest,
            "symbols": [symbol.record(path.name)], "start_line": start, "end_line": start - 1,
            "source": "", "next_start_line": None, "truncated": bool(source.reason),
            "reason": source.reason,
            "hint": "Continue with next_start_line + expected_hash; use read_file for long lines.",
        }
        # 只按 LF 计行，与 Tree-sitter 一致；字符串里的 Unicode 分隔符不是源码换行。
        parts = source.text.split("\n")
        rows = [line + "\n" for line in parts[:-1]] + parts[-1:]
        for number in range(start, symbol.end_line + 1):
            if number - start >= params.max_lines:
                result.update(next_start_line=number, truncated=True, reason="page_limit")
                break
            candidate = {**result, "end_line": number,
                         "source": str(result["source"]) + rows[number - 1]}
            # 预留续读元数据，避免最后补上截断原因后才超过上限。
            if not fits_result({**candidate, "next_start_line": number + 1,
                                "truncated": True, "reason": "output_limit"}):
                if number == start:
                    raise ToolError("source line exceeds output limit; use read_file",
                                    "resource_limit")
                result.update(next_start_line=number, truncated=True, reason="output_limit")
                break
            result = candidate
        return json_result(result)
