from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from kama_claude.core.tools.errors import ToolError
from kama_claude.core.workspace import WorkspacePathError

MAX_FILE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024


@dataclass
class ReadBudget:
    max_bytes: int = 20 * 1024 * 1024
    deadline: float = field(default_factory=lambda: time.monotonic() + 5)
    consumed: int = 0
    reason: str = ""

    # 扫描与解码共享有界预算，达到上限返回部分结果并说明原因
    def check(self, size: int = 0) -> bool:
        self.consumed += size
        if time.monotonic() >= self.deadline:
            self.reason = "time_limit"
        elif self.consumed > self.max_bytes:
            self.reason = "scan_bytes_limit"
        return not self.reason


@dataclass(frozen=True)
class TextLine:
    number: int
    text: str
    shortened: bool


# 防止在线程开始读取前，原本规范化的目标又变成链接
def check_target(path: Path) -> None:
    if path.resolve() != path:
        raise WorkspacePathError("target path changed; call the tool again for approval")


# 分段读取并排空超长行，内存占用不随原文件或单行长度无限增长
def text_lines(path: Path, budget: ReadBudget, line_limit: int = 65_536) -> Iterator[TextLine]:
    check_target(path)
    if path.exists() and not path.is_file():
        raise ToolError(f"target is not a regular text file: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="\n") as stream:
            sample = stream.read(4096)
            if not budget.check(len(sample.encode("utf-8"))):
                return
            if "\0" in sample:
                raise ToolError(f"binary file is not supported: {path}")
            stream.seek(0)
            number = 0
            while budget.check():
                chunk = stream.readline(line_limit + 1)
                if not chunk:
                    return
                if "\0" in chunk:
                    raise ToolError(f"binary file is not supported: {path}")
                if not budget.check(len(chunk.encode("utf-8"))):
                    return
                number += 1
                shortened = len(chunk.rstrip("\r\n")) > line_limit
                text = chunk[:line_limit].rstrip("\r\n")
                while chunk and not chunk.endswith("\n") and len(chunk) > line_limit:
                    chunk = stream.readline(line_limit + 1)
                    if "\0" in chunk:
                        raise ToolError(f"binary file is not supported: {path}")
                    if not budget.check(len(chunk.encode("utf-8"))):
                        return
                yield TextLine(number, text, shortened)
    except UnicodeError as exc:
        raise ToolError(f"file is not valid UTF-8 text: {path}") from exc


# 编辑前完整读取有明确大小上限的文本，不支持二进制或隐式编码替换
def read_editable(path: Path) -> bytes:
    check_target(path)
    if path.exists() and not path.is_file():
        raise ToolError(f"target is not a regular text file: {path}")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise ToolError(f"file not found: {path}", "not_found") from exc
    except PermissionError as exc:
        raise ToolError(str(exc), "permission_denied") from exc
    except IsADirectoryError as exc:
        raise ToolError(str(exc)) from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ToolError("file exceeds the 1 MiB editing limit", "resource_limit")
    if b"\0" in raw:
        raise ToolError("binary files cannot be edited")
    try:
        raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ToolError("file is not valid UTF-8 text") from exc
    return raw


# 提交前的编码与大小错误属于明确前置错误，不是未知副作用
def encode_text(content: str) -> bytes:
    try:
        encoded = content.encode("utf-8")
    except UnicodeError as exc:
        raise ToolError("content is not valid UTF-8 text") from exc
    if len(encoded) > MAX_FILE_BYTES:
        raise ToolError("content exceeds the 1 MiB writing limit", "resource_limit")
    return encoded


# 同目录临时文件完整落盘后替换；编辑额外核对原始内容，保留现有文件权限
def atomic_write(path: Path, content: bytes, *, expected: bytes | None = None) -> None:
    check_target(path)
    if path.exists() and not path.is_file():
        raise ToolError(f"target is not a regular text file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".kama-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        check_target(path)
        if expected is not None:
            try:
                with path.open("rb") as original:
                    current = original.read(MAX_FILE_BYTES + 1)
            except FileNotFoundError as exc:
                raise ToolError("file changed before commit; read it again", "conflict") from exc
            if current != expected:
                raise ToolError("file changed before commit; read it again", "conflict")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
