from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from kama_claude.core.memory.model import MemoryEntry, MemoryError, MemoryScope

_KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")


class MemoryStore:
    # 只绑定三层目录，读取和查看不会创建文件；一个 Core 内同步读写不会交错
    def __init__(
        self, workspace: Path, session_dir: Path | None = None, *,
        global_dir: Path | None = None,
    ) -> None:
        self._roots: dict[MemoryScope, Path] = {
            "global": global_dir or Path.home() / ".kama" / "memory",
            "project": workspace / ".kama" / "memory",
        }
        if session_dir is not None:
            self._roots["session"] = session_dir / "memory"

    @property
    # 判断是否绑定了会话目录，纯内存运行不注入会话记忆
    def has_session(self) -> bool:
        return "session" in self._roots

    # 返回当前作用域的固定目录，不接受模型传入任意路径
    def directory(self, scope: MemoryScope) -> Path:
        if scope not in self._roots:
            raise MemoryError(f"memory scope unavailable: {scope}")
        return self._roots[scope].expanduser().resolve()

    # 检查文件名、Windows 保留名称和软链接，阻止跨作用域路径访问
    def _path(self, scope: MemoryScope, key: str) -> Path:
        if not _KEY.fullmatch(key) or PureWindowsPath(key + ".md").is_reserved():
            raise MemoryError("invalid memory key; use 1-64 letters, digits, _, - or dots")
        path = self.directory(scope) / (key + ".md")
        if path.is_symlink():
            raise MemoryError(f"memory symlinks are not supported: {key}")
        return path

    # 读取固定头部，分隔线之后的标题、代码块等都只是正文
    def get(self, scope: MemoryScope, key: str) -> MemoryEntry:
        path = self._path(scope, key)
        try:
            text = path.read_text(encoding="utf-8")
            header, separator, content = text.partition("\n\n---\n\n")
            lines = header.splitlines()
            prefixes = ("- 摘要：", "- 固定：", "- 更新时间：", "- 来源：")
            if (
                not separator or len(lines) != 6 or lines[:2] != [f"# {key}", ""]
                or any(not line.startswith(prefix) for line, prefix in zip(lines[2:], prefixes))
            ):
                raise MemoryError(f"invalid Markdown memory header: {path}")
            summary, pinned, updated_at, source = [
                line[len(prefix):].strip() for line, prefix in zip(lines[2:], prefixes)
            ]
            if pinned not in ("是", "否") or not source:
                raise MemoryError(f"invalid memory metadata: {path}")
            timestamp = datetime.fromisoformat(updated_at)
            if timestamp.tzinfo is None:
                raise MemoryError(f"memory timestamp must include timezone: {path}")
            return MemoryEntry(
                key=key, summary=summary, pinned=pinned == "是",
                updated_at=updated_at, source=source, content=content.strip(),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise MemoryError(f"cannot read memory {scope}/{key}: {exc}") from exc

    # 扫描有效条目并收集坏文件信息；一个坏文件不会隐藏其余有效记忆
    def scan(self, scope: MemoryScope) -> tuple[list[MemoryEntry], list[str]]:
        entries: list[MemoryEntry] = []
        warnings: list[str] = []
        try:
            root = self.directory(scope)
            for path in sorted(root.glob("*.md")):
                try:
                    entries.append(self.get(scope, path.stem))
                except MemoryError as exc:
                    warnings.append(str(exc))
        except OSError as exc:
            warnings.append(f"cannot list memory {scope}: {exc}")
        entries.sort(key=lambda entry: (
            not entry.pinned, -datetime.fromisoformat(entry.updated_at).timestamp(), entry.key,
        ))
        return entries, warnings

    # 列表和关键词查找只返回索引，不把全部正文送给模型
    def list(
        self, scope: MemoryScope, *, query: str = "", offset: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 50:
            raise MemoryError("invalid memory pagination")
        entries, warnings = self.scan(scope)
        needle = query.strip().casefold()
        if needle:
            entries = [
                entry for entry in entries
                if needle in (entry.key + "\n" + entry.summary + "\n" + entry.content).casefold()
            ]
        page = entries[offset:offset + limit]
        return {
            "scope": scope, "path": str(self.directory(scope)),
            "entries": [entry.model_dump(exclude={"content"}) for entry in page],
            "total": len(entries), "warnings": warnings,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }

    # 按字符位置读取正文，超长手工 Markdown 也能逐段查看
    def read(self, scope: MemoryScope, key: str, *, offset: int = 0) -> dict[str, Any]:
        if offset < 0:
            raise MemoryError("memory offset must be nonnegative")
        entry = self.get(scope, key)
        end = offset + 4_000
        return {
            **entry.model_dump(exclude={"content"}), "scope": scope,
            "content": entry.content[offset:end], "total_chars": len(entry.content),
            "next_offset": end if end < len(entry.content) else None,
        }

    # 相同 key 更新原文件；完全相同的内容不改来源和时间，也不重复写入
    def upsert(
        self, scope: MemoryScope, key: str, *, summary: str, content: str,
        source: str, pinned: bool | None = None,
    ) -> dict[str, Any]:
        path = self._path(scope, key)
        summary, content = summary.strip(), content.strip()
        if (
            not summary or len(summary) > 160 or "\n" in summary or "\r" in summary
            or not content or len(content) > 4_000
            or not source or "\n" in source or "\r" in source
        ):
            raise MemoryError("summary must be one line (1-160 chars); body must be 1-4000 chars")
        previous = self.get(scope, key) if path.exists() else None
        effective_pin = pinned if pinned is not None else previous.pinned if previous else False
        if previous and (
            previous.summary, previous.content, previous.pinned
        ) == (summary, content, effective_pin):
            return {"status": "unchanged", "scope": scope, "key": key}
        entry = MemoryEntry(
            key=key, summary=summary, content=content, pinned=effective_pin,
            updated_at=datetime.now(UTC).isoformat(), source=source,
        )
        text = (
            f"# {key}\n\n- 摘要：{entry.summary}\n- 固定：{'是' if entry.pinned else '否'}\n"
            f"- 更新时间：{entry.updated_at}\n- 来源：{entry.source}\n\n---\n\n{entry.content}\n"
        )
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
        except OSError as exc:
            raise MemoryError(f"cannot save memory {scope}/{key}: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"status": "updated" if previous else "created", "scope": scope, "key": key}

    # 删除前确认格式，损坏文件留给用户检查；重复删除不报错
    def delete(self, scope: MemoryScope, key: str) -> dict[str, Any]:
        path = self._path(scope, key)
        if not path.exists():
            return {"status": "already_absent", "scope": scope, "key": key}
        self.get(scope, key)
        try:
            path.unlink()
        except OSError as exc:
            raise MemoryError(f"cannot delete memory {scope}/{key}: {exc}") from exc
        return {"status": "deleted", "scope": scope, "key": key}
