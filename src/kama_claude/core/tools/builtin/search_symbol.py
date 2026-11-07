from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import Field

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import ReadBudget
from kama_claude.core.tools.output import json_result
from kama_claude.core.tools.symbols import EXTENSIONS, SymbolKind, parse_source
from kama_claude.core.tools.traversal import DirectoryScanner
from kama_claude.core.workspace import WorkspacePathError


class SearchSymbolParams(ToolParams):
    name: str = Field(min_length=1, max_length=256,
                      description="Case-sensitive simple or qualified name, e.g. Service.handle.")
    path: str = Field(default=".", min_length=1, description="Directory or concrete source file.")
    match: Literal["exact", "partial"] = Field(default="exact", description="Name matching mode.")
    kind: SymbolKind | None = Field(default=None, description="Optional definition kind filter.")
    limit: int = Field(default=30, ge=1, le=200, description="Maximum definition records.")
    include_ignored: bool = Field(default=False, description="Include ignored/generated files.")


class SearchSymbolTool(FileTool):
    name = "search_symbol"
    retry_safe = True
    params_model = SearchSymbolParams
    input_schema = SearchSymbolParams.model_json_schema()
    description = (
        "Locate Python, JS/JSX and TS/TSX definitions by case-sensitive simple or qualified name. "
        "Returns JSON paths, signatures and inclusive line ranges; multiple namesakes are "
        "candidates, not a resolved reference. Use read_symbol on a concrete path and name "
        "(plus its start_line if ambiguous). Does not find usages or resolve imports. "
        "Skips links and ignored/generated entries by default. Bounded scans report partial "
        "coverage; no matches in a partial scan do not prove absence. Narrow path/name when "
        "truncated, or use search_text for unsupported languages. External paths follow approvals."
    )

    async def _invoke_path(self, params: dict[str, object], root: Path) -> ToolResult:
        parsed = SearchSymbolParams.model_validate(params)
        return await asyncio.to_thread(self._search, root, parsed)

    def _search(self, root: Path, params: SearchSymbolParams) -> ToolResult:
        if not root.exists():
            raise FileNotFoundError(str(root))
        directory = root.is_dir()
        base = root if directory else root.parent
        budget = ReadBudget()
        scanner = DirectoryScanner(root, self.paths.root, params.include_ignored, budget)
        files = scanner.files() if directory else iter([root])
        records: list[dict[str, object]] = []
        reasons: set[str] = set()
        scanned = skipped = unsupported = 0
        for path in files:
            if directory and path.suffix.lower() not in EXTENSIONS:
                unsupported += 1
                continue
            try:
                source = parse_source(path, budget)
            except WorkspacePathError:
                raise
            except (ToolError, OSError) as exc:
                if not directory:
                    raise
                skipped += 1
                reasons.add(exc.error_type if isinstance(exc, ToolError) else "unreadable_file")
                if not budget.check():
                    break
                continue
            scanned += 1
            if source.reason:
                reasons.add(source.reason)
            for symbol in source.symbols:
                if ((params.kind is None or symbol.kind == params.kind)
                        and symbol.matches(params.name, params.match == "partial")):
                    if len(records) >= params.limit:
                        reasons.add("result_limit")
                        break
                    records.append(symbol.record(path.relative_to(base).as_posix()))
            if "result_limit" in reasons or not budget.check():
                break
        if budget.reason or scanner.reason:
            reasons.add(budget.reason or scanner.reason)
        if scanner.skipped:
            reasons.add("unreadable_directories")
        return json_result({
            "root": str(base), "symbols": records, "truncated": bool(reasons),
            "reason": ",".join(sorted(reasons)) or None, "scanned_files": scanned,
            "skipped_files": skipped, "unsupported_files": unsupported,
            "skipped_directories": scanner.skipped,
            "hint": "Narrow path/name if partial; use read_symbol for a candidate's body.",
        }, "symbols")
