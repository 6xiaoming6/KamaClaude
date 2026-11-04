from __future__ import annotations

from pathlib import Path

from pydantic import Field

from kama_claude.core.tools.base import FileTool, ToolParams, ToolResult
from kama_claude.core.tools.files import atomic_write, encode_text


class WriteFileParams(ToolParams):
    path: str
    content: str = Field(max_length=1024 * 1024)


class WriteFileTool(FileTool):
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Write text content to a file, creating it (and any parent directories) if it "
        "does not exist, or overwriting it if it does. "
        "Prefer edit_file for local changes to an existing file. "
        "Relative paths use the fixed workspace; absolute paths are accepted and external paths "
        "require approval. Content is limited to 1 MiB and committed by atomic replacement."
    )
    input_schema = WriteFileParams.model_json_schema()

    # 路径已通过校验，再检查大小、创建父目录并写入
    async def _invoke_path(self, params: dict[str, object], path: Path) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        encoded = encode_text(p.content)
        atomic_write(path, encoded)

        return ToolResult(content=f"wrote {len(encoded)} bytes to {path_str}")
