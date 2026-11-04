from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import MAX_FILE_BYTES, atomic_write, encode_text, read_editable


class EditFileParams(ToolParams):
    path: str
    old_text: str = Field(min_length=1, max_length=MAX_FILE_BYTES)
    new_text: str = Field(max_length=MAX_FILE_BYTES)

    @model_validator(mode="after")
    # 空匹配和无变化编辑在进入审批前拒绝
    def validate_change(self) -> Self:
        if self.old_text == self.new_text:
            raise ValueError("new_text must differ from old_text")
        return self


class EditFileTool(FileTool):
    name = "edit_file"
    description = (
        "Replace exactly one occurrence of old_text in an existing UTF-8 text file, then return "
        "a short unified diff. Read the file first; omit displayed line numbers from old_text. "
        "No fuzzy or multiple-match edits. Preserves UTF-8 BOM and consistent CRLF line endings. "
        "Checks for concurrent content changes before atomic commit; file limit is 1 MiB. "
        "Relative paths use the workspace; external paths require one-time approval."
    )
    params_model = EditFileParams
    input_schema = EditFileParams.model_json_schema()

    # 精确匹配后才生成新内容，提交前核对原文，失败不覆盖文件
    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult:
        parsed = EditFileParams.model_validate(params)
        raw = read_editable(path)
        before = raw.decode("utf-8-sig")
        old, new = parsed.old_text, parsed.new_text
        # 显示的行号与换行符不属于代码；纯 CRLF 文件只对换行形式做确定性对齐
        if "\r\n" in before and before.count("\r\n") == before.count("\n"):
            old = old.replace("\r\n", "\n").replace("\n", "\r\n")
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        position = before.find(old)
        if position < 0 or before.find(old, position + 1) >= 0:
            raise ToolError(
                "old_text must match exactly once; read again and narrow the matching text",
                "conflict",
            )
        if old == new:
            raise ToolError("line-ending alignment leaves no content change")
        after = before.replace(old, new, 1)
        encoded = encode_text(after)
        if raw.startswith(b"\xef\xbb\xbf"):
            encoded = b"\xef\xbb\xbf" + encoded
        if len(encoded) > MAX_FILE_BYTES:
            raise ToolError("edited file exceeds 1 MiB", "resource_limit")
        difference = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile=str(path), tofile=str(path),
            lineterm="", n=3,
        ))
        preview = "\n".join(difference[:200])
        truncated = len(difference) > 200 or len(preview) > 12_000
        preview = preview[:12_000]
        atomic_write(path, encoded, expected=raw)
        return ToolResult(content=json.dumps({
            "path": str(path), "replacements": 1, "diff": preview, "diff_truncated": truncated,
        }, ensure_ascii=False))
