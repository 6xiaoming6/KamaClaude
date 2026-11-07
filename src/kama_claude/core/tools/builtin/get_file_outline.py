from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import Field

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.files import ReadBudget
from kama_claude.core.tools.output import json_result
from kama_claude.core.tools.symbols import parse_source


class FileOutlineParams(ToolParams):
    path: str = Field(min_length=1, description="Concrete Python, JS/JSX or TS/TSX file.")
    limit: int = Field(default=100, ge=1, le=200, description="Maximum definition records.")


class GetFileOutlineTool(FileTool):
    name = "get_file_outline"
    retry_safe = True
    params_model = FileOutlineParams
    input_schema = FileOutlineParams.model_json_schema()
    description = (
        "Inspect one Python, JS/JSX or TS/TSX file before reading its bodies. Returns bounded "
        "JSON definitions, qualified names, kinds, signatures, inclusive line ranges and file "
        "hash. Use read_symbol(path, name) for a body or read_file for arbitrary lines. "
        "Syntax definitions only, not a call graph. Partial parsing and output limits are "
        "reported; narrow the query with search_symbol when truncated. Relative paths use "
        "the workspace; external paths follow file approvals. Does not scan the repository."
    )

    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult:
        parsed = FileOutlineParams.model_validate(params)
        return await asyncio.to_thread(self._outline, path, parsed)

    def _outline(self, path: Path, params: FileOutlineParams) -> ToolResult:
        source = parse_source(path, ReadBudget())
        reason = source.reason or ("result_limit" if len(source.symbols) > params.limit else None)
        return json_result({
            "root": str(path.parent), "file_hash": source.digest,
            "symbols": [symbol.record(path.name) for symbol in source.symbols[:params.limit]],
            "truncated": bool(reason), "reason": reason,
            "hint": "Use search_symbol with a name or read_file with a line range to inspect more.",
        }, "symbols")
