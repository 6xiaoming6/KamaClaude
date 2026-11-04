from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.checkpoint.checkpoint import Checkpoint, CheckpointStore
from kama_claude.core.session.model import Session

logger = logging.getLogger(__name__)
MessageContent = str | list[dict[str, Any]]


# 返回当前 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 创建会话根目录并接入权威检查点存储
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        self._root.mkdir(parents=True, exist_ok=True)
        # 每个session需要管理自己的checkpoint
        self.checkpoints = CheckpointStore(self._root)

    # 返回会话目录并验证标识不会逃出根目录
    def session_dir(self, sid: str) -> Path:
        self.checkpoints.path(sid)
        return self._root / sid

    # 返回会话运行记录目录
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 列出具有历史数据的会话目录(扫描本地目录的文件)
    def session_ids(self) -> list[str]:
        return [
            p.name
            for p in self._root.iterdir()
            if p.is_dir() and ((p / "meta.json").exists() or (p / "checkpoint.json").exists())
        ]

    # 导入旧会话的合法历史，已有快照时不允许回退到旧文件
    def ensure_checkpoint(self, sid: str) -> Checkpoint:
        if self.checkpoints.exists(sid):
            return self.checkpoints.load(sid)
        session = self.read_meta(sid)
        cp = Checkpoint(session=session.to_dict(), messages=self.read_messages(sid))
        self.checkpoints.save(cp)
        return cp

    # 保存权威元数据并尽力更新兼容文件
    def write_meta(self, session: Session) -> None:
        sid = session.id
        if self.checkpoints.exists(sid):
            cp = self.checkpoints.load(sid)
            cp.session = session.to_dict()
        else:
            cp = Checkpoint(session=session.to_dict(), messages=self.read_messages(sid))
        self.checkpoints.save(cp)
        self.export_meta(session)

    # 只导出兼容元数据，不再次提交检查点
    def export_meta(self, session: Session) -> None:
        sid = session.id
        try:
            path = self.session_dir(sid)
            path.mkdir(parents=True, exist_ok=True)
            (path / "meta.json").write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.exception("cannot export session metadata sid=%s", sid)

    # 优先从权威快照读取元数据
    def read_meta(self, sid: str) -> Session:
        if self.checkpoints.exists(sid):
            return Session.from_dict(self.checkpoints.load(sid).session)
        data = json.loads((self.session_dir(sid) / "meta.json").read_text(encoding="utf-8"))
        return Session.from_dict(data)

    # 向权威历史追加消息，旧会话在首次打开前仍兼容原格式
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        if self.checkpoints.exists(sid):
            cp = self.checkpoints.load(sid)
            cp.messages.append({"role": role, "content": content})
            self.checkpoints.save(cp)
            self.export_messages(sid, cp.messages, run_id=run_id)
            return
        row: dict[str, Any] = {"ts": _now(), "role": role, "content": content}
        if run_id is not None:
            row["run_id"] = run_id
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "thread.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 兼容旧调用方的批量追加接口
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(sid, str(msg["role"]), msg["content"], run_id)

    # 导出兼容历史，导出失败不改变已提交的权威状态
    def export_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str | None = None,
    ) -> None:
        try:
            path = self.session_dir(sid)
            path.mkdir(parents=True, exist_ok=True)
            with (path / "thread.jsonl").open("w", encoding="utf-8") as f:
                for msg in messages:
                    row = {"ts": _now(), **msg}
                    if run_id:
                        row["run_id"] = run_id
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("cannot export thread sid=%s", sid)

    # 快照保留原始工作历史，仅旧数据执行兼容性裁剪
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        if self.checkpoints.exists(sid):
            return self.checkpoints.load(sid).messages
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []
        messages: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("invalid message")
            except (ValueError, TypeError):
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            if row.get("role") not in ("user", "assistant"):
                continue
            messages.append({"role": row["role"], "content": row.get("content", "")})
        from kama_claude.core.compact.budget import truncate_tool_results

        return truncate_tool_results(self._trim_orphan_tool_use(messages))

    # 仅在导入旧历史时裁去尾部孤立调用
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if msg["role"] == "assistant" and block.get("type") == "tool_use":
                        pending.add(str(block.get("id", "")))
                    elif msg["role"] == "user" and block.get("type") == "tool_result":
                        pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        return messages[:last_balanced] if pending else messages

    # 先提交压缩后的权威历史，再更新备份和兼容输出
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        cp = self.ensure_checkpoint(sid)
        cp.messages = messages
        self.checkpoints.save(cp)
        path = self.session_dir(sid) / "thread.jsonl"
        if path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            try:
                path.rename(path.with_name(f"thread_{stamp}.jsonl.bak"))
            except OSError:
                logger.exception("cannot backup legacy thread sid=%s", sid)
        self.export_messages(sid, messages)

    # 读取会话笔记
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # 将笔记追加到独立文件
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
