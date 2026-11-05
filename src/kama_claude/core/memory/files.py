from __future__ import annotations

import os
import tempfile
from pathlib import Path

from kama_claude.core.memory.model import MemoryError


# 缺失文件视为空，编码和权限问题必须暴露，不能悄悄丢掉用户规则。
def read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise MemoryError(f"cannot read {path}: {exc}") from exc


# 完整写入同目录临时文件后替换；任何失败都清理临时文件并向上报告。
def atomic_write(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=".memory-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, UnicodeError) as exc:
        raise MemoryError(f"cannot save {path}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
