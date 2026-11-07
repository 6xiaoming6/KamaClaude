from __future__ import annotations

from pathlib import Path

from pydantic import Field

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.files import ReadBudget
from kama_claude.core.tools.traversal import DirectoryScanner, is_link

_MAX_DEPTH = 4
_MAX_ENTRIES = 200


class ListDirParams(ToolParams):
    path: str = Field(default=".", min_length=1, description="Directory to list.")
    max_depth: int = Field(default=2, ge=1, le=_MAX_DEPTH,
                           description="Displayed levels below path; not a full recursive scan.")
    include_ignored: bool = Field(default=False, description="Include ignored/generated entries.")


class ListDirTool(FileTool):
    retry_safe = True
    params_model = ListDirParams
    name = "list_dir"
    description = (
        "List the contents of a directory as a tree. "
        "Relative paths use the fixed workspace; external absolute paths require approval. "
        "Skips common generated directories and .gitignore matches unless include_ignored=true. "
        "Other hidden entries are shown. Links are displayed but never recursively followed. "
        f"Maximum depth is {_MAX_DEPTH}, maximum total entries is {_MAX_ENTRIES}. "
        "Directories at the depth boundary are marked as not expanded. "
        "Use find_files/search_text to locate files without expanding a large tree."
    )
    input_schema = ListDirParams.model_json_schema()

    # 列举已校验目录，保留深度和条数上限但不跟随链接
    async def _invoke_path(self, params: dict[str, object], root: Path) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path_str = p.path
        max_depth = p.max_depth
        budget = ReadBudget()
        scanner = DirectoryScanner(root, self.paths.root, p.include_ignored, budget)

        if not root.exists():
            raise FileNotFoundError(f"no such directory: {path_str}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines: list[str] = [str(root) + "/"]
        count = 0

        # 递归仅进入普通目录，链接只展示名称
        def _walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal count
            if depth > max_depth or count >= _MAX_ENTRIES or not budget.check() or scanner.reason:
                return
            entries = scanner.entries(directory)
            for i, entry in enumerate(entries):
                if count >= _MAX_ENTRIES:
                    lines.append(f"{prefix}... (truncated)")
                    return
                connector = "└── " if i == len(entries) - 1 else "├── "
                linked = is_link(entry)
                is_dir = not linked and entry.is_dir()
                suffix = " [link]" if linked else "/" if is_dir else ""
                if is_dir and depth == max_depth:
                    suffix += " [not expanded: max_depth]"
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                count += 1
                if is_dir and depth < max_depth:
                    extension = "    " if i == len(entries) - 1 else "│   "
                    _walk(entry, depth + 1, prefix + extension)

        _walk(root, 1, "")
        if budget.reason or scanner.reason:
            lines.append(f"[truncated: {budget.reason or scanner.reason}]")
        return ToolResult(content="\n".join(lines))
