from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.config import CompactionConfig, MemoryConfig
from kama_claude.core.memory.files import atomic_write, read_optional
from kama_claude.core.memory.model import MemoryEntry, MemoryError, MemoryType
from kama_claude.core.memory.paths import checked_path, project_id, project_root

_KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")


class MemoryStore:
    # 绑定固定项目和用户目录；同步方法在单 Core 内串行完成，不新增执行状态。
    def __init__(
        self, workspace: Path, config: MemoryConfig | None = None, *, home: Path | None = None,
        project: Path | None = None,
        context_budget: CompactionConfig | None = None,
    ) -> None:
        self.home = (home or Path.home() / ".kama").expanduser().resolve()
        self.project_root = (
            project.resolve(strict=True) if project is not None else project_root(workspace)
        )
        self.project_id = project_id(self.project_root)
        self.root = self.home / "projects" / self.project_id / "memory"
        self.config = config or MemoryConfig()
        self._context_budget = context_budget or CompactionConfig()

    def _budget(self, ratio: float) -> int:
        return max(1, int(self._context_budget.context_window_tokens * ratio))

    # 自动文件只能经专用入口维护，拒绝链接到用户规则或其他存储位置。
    def global_path(self) -> Path:
        return checked_path(self.home / "AGENTS.auto.md", self.home)

    # 自动注入读取全文并检查预算；工具读取仍可查看超预算文件以便修复。
    def global_text(self) -> str:
        text = read_optional(self.global_path())
        self._check_budget(
            text, self._budget(self.config.global_auto_ratio), "global automatic memory",
        )
        return text

    # key 不能选择目录、索引或 Windows 保留文件名。
    def path(self, key: str) -> Path:
        if (
            not _KEY.fullmatch(key) or key.casefold() == "memory"
            or PureWindowsPath(key + ".md").is_reserved()
        ):
            raise MemoryError("invalid memory key; use 1-64 letters, digits, _, - or dots")
        return checked_path(self.root / (key + ".md"), self.home)

    # 预算不够时要求维护者精简，不悄悄裁掉已经记录的约定或索引。
    def _check_budget(self, text: str, budget: int, label: str) -> None:
        if estimate_bytes(text) > budget:
            raise MemoryError(
                f"{label} exceeds {budget} estimated tokens; merge or shorten entries",
            )

    # 读取固定头部；正文中的标题或代码不参与元数据解析。
    def get(self, key: str) -> MemoryEntry:
        path = self.path(key)
        try:
            text = path.read_text(encoding="utf-8")
            header, separator, content = text.partition("\n\n---\n\n")
            lines = header.splitlines()
            prefixes = ("- 摘要：", "- 类型：", "- 更新时间：", "- 来源：")
            if (
                not separator or len(lines) != 6 or lines[:2] != [f"# {key}", ""]
                or any(not line.startswith(prefix) for line, prefix in zip(lines[2:], prefixes))
            ):
                raise MemoryError(f"invalid Markdown memory header: {path}")
            summary, kind, updated_at, source = [
                line[len(prefix):].strip() for line, prefix in zip(lines[2:], prefixes)
            ]
            if datetime.fromisoformat(updated_at).tzinfo is None or not source:
                raise MemoryError(f"invalid memory metadata: {path}")
            return MemoryEntry.model_validate({
                "key": key, "summary": summary, "type": kind, "updated_at": updated_at,
                "source": source, "content": content.strip(),
            })
        except (OSError, UnicodeError, ValueError) as exc:
            kind = "not_found" if isinstance(exc, FileNotFoundError) else "invalid_operation"
            raise MemoryError(f"cannot read project memory {key}: {exc}", kind) from exc

    # 正文是权威来源；坏文件明确报错，避免重建索引时静默丢失条目。
    def scan(self) -> list[MemoryEntry]:
        root = checked_path(self.root, self.home)
        try:
            paths = list(root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise MemoryError(f"cannot list memory: {exc}") from exc
        entries = [
            self.get(path.stem) for path in paths
            if path.suffix.casefold() == ".md" and path.stem.casefold() != "memory"
        ]
        return sorted(entries, key=lambda entry: (entry.type, entry.key))

    # 索引只有类型、简短概要和相对文件名，不复制任何正文。
    def _render_index(self, entries: list[MemoryEntry]) -> str:
        text = "# 项目记忆索引\n\n" + "".join(
            f"- [{entry.key}]({entry.key}.md) ({entry.type})：{entry.summary}\n"
            for entry in sorted(entries, key=lambda item: (item.type, item.key))
        ) if entries else ""
        self._check_budget(text, self._budget(self.config.project_index_ratio), "project index")
        return text

    # 缺失或陈旧索引先修复，再用于读取或注入；空目录不会因查询而创建。
    def index(self, entries: list[MemoryEntry] | None = None) -> str:
        text = self._render_index(self.scan() if entries is None else entries)
        path = checked_path(self.root / "MEMORY.md", self.home)
        if read_optional(path) != text:
            atomic_write(path, text)
        return text

    # 列表和关键词查找返回索引元数据；正文按 key 再读。
    def list(self, *, query: str = "", offset: int = 0, limit: int = 20) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 50:
            raise MemoryError("invalid memory pagination")
        entries = self.scan()
        index = self.index(entries)
        needle = query.strip().casefold()
        if needle:
            entries = [entry for entry in entries if needle in (
                entry.key + "\n" + entry.summary + "\n" + entry.content
            ).casefold()]
        return {
            "project_id": self.project_id, "path": str(self.root), "index": index,
            "entries": [entry.model_dump(exclude={"content"})
                        for entry in entries[offset:offset + limit]],
            "total": len(entries),
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }

    # 按字符位置读取详情；需要时也会先修复索引。
    def read(self, key: str, *, offset: int = 0) -> dict[str, Any]:
        if offset < 0:
            raise MemoryError("memory offset must be nonnegative")
        entry = self.get(key)
        self.index()
        end = offset + 4_000
        return {
            **entry.model_dump(exclude={"content"}), "path": str(self.path(key)),
            "content": entry.content[offset:end], "total_chars": len(entry.content),
            "next_offset": end if end < len(entry.content) else None,
        }

    # 全局 hash 基于实际文件字节，缺失文件也有稳定的空内容 hash。
    def read_global(self, *, offset: int = 0) -> dict[str, Any]:
        path = self.global_path()
        try:
            data = path.read_bytes() if path.exists() else b""
            text = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise MemoryError(f"cannot read global automatic memory: {exc}") from exc
        if offset < 0:
            raise MemoryError("memory offset must be nonnegative")
        end = offset + 4_000
        return {
            "path": str(path), "content": text[offset:end], "total_chars": len(text),
            "expected_hash": hashlib.sha256(data).hexdigest(),
            "next_offset": end if end < len(text) else None,
        }

    # 全局更新必须先读取并携带 hash，避免多个 Agent 用旧内容覆盖彼此。
    def write_global(self, content: str | None, expected_hash: str) -> dict[str, Any]:
        current = self.read_global()
        path = self.global_path()
        if content is not None:
            self._check_budget(
                content, self._budget(self.config.global_auto_ratio), "global automatic memory",
            )
        if content is None and not path.exists():
            return {"status": "already_absent", "scope": "global"}
        if content is not None and current["content"] == content and (
            current["total_chars"] == len(content)
        ):
            return {"status": "unchanged", "scope": "global"}
        if expected_hash != current["expected_hash"]:
            raise MemoryError(
                "global memory changed; read it again before writing", "edit_conflict",
            )
        if content is None:
            try:
                path.unlink()
            except OSError as exc:
                raise MemoryError(f"cannot delete global automatic memory: {exc}") from exc
            return {"status": "deleted", "scope": "global"}
        atomic_write(path, content)
        return {"status": "updated", "scope": "global"}

    # 先验证完整候选索引预算，再提交正文；同 key 内容不变时不改来源或时间。
    def upsert(
        self, key: str, *, summary: str, content: str, source: str, type: MemoryType = "project",
    ) -> dict[str, Any]:
        path = self.path(key)
        summary, content = summary.strip(), content.strip()
        if not summary or len(summary) > 160 or "\n" in summary or "\r" in summary:
            raise MemoryError("summary must be one line (1-160 chars)")
        if not content or len(content) > 4_000 or not source or "\n" in source or "\r" in source:
            raise MemoryError("body must be 1-4000 chars and source must be one line")
        previous = self.get(key) if path.exists() else None
        entries = [entry for entry in self.scan() if entry.key != key]
        if previous and (previous.summary, previous.content, previous.type) == (
            summary, content, type,
        ):
            self.index([*entries, previous])
            return {"status": "unchanged", "scope": "project", "key": key}
        entry = MemoryEntry(
            key=key, summary=summary, content=content, type=type,
            updated_at=datetime.now(UTC).isoformat(), source=source,
        )
        self._render_index([*entries, entry])
        text = (
            f"# {key}\n\n- 摘要：{entry.summary}\n- 类型：{entry.type}\n"
            f"- 更新时间：{entry.updated_at}\n- 来源：{entry.source}\n\n---\n\n{entry.content}\n"
        )
        atomic_write(path, text)
        try:
            self.index([*entries, entry])
        except MemoryError as exc:
            raise MemoryError(
                f"body saved, index update failed; next read will repair it: {exc}",
            ) from exc
        return {"status": "updated" if previous else "created", "scope": "project", "key": key}

    # 删除与索引提交按顺序完成；失败会说明正文已经删除，重复调用安全。
    def delete(self, key: str) -> dict[str, Any]:
        path = self.path(key)
        entries = self.scan()
        if not path.exists():
            self.index(entries)
            return {"status": "already_absent", "scope": "project", "key": key}
        try:
            path.unlink()
        except OSError as exc:
            raise MemoryError(f"cannot delete memory: {exc}") from exc
        try:
            self.index([entry for entry in entries if entry.key != key])
        except MemoryError as exc:
            raise MemoryError(
                f"body deleted, index update failed; next read will repair it: {exc}",
            ) from exc
        return {"status": "deleted", "scope": "project", "key": key}
