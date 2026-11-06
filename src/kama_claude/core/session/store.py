from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from kama_claude.core.checkpoint.checkpoint import Checkpoint, CheckpointError, CheckpointStore
from kama_claude.core.session.model import Session

logger = logging.getLogger(__name__)
MessageContent = str | list[dict[str, Any]]
_HISTORY_PAGE_BYTES = 1024 * 1024
_HISTORY_PAGE_MESSAGES = 200


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

    # 导入旧会话或补齐已提交但尚未追加的原文，不用完整历史覆盖模型工作上下文
    def ensure_checkpoint(self, sid: str) -> Checkpoint:
        if self.checkpoints.exists(sid):
            cp = self.checkpoints.load(sid)
            if not cp.history_initialized:
                self.save_checkpoint(cp)
            else:
                self.flush_history(cp)
            return cp
        session = self.read_meta(sid)
        cp = Checkpoint(session=session.to_dict(), messages=self.read_messages(sid))
        self.save_checkpoint(cp)
        return cp

    # 保存权威元数据并尽力更新兼容文件
    def write_meta(self, session: Session) -> None:
        sid = session.id
        if self.checkpoints.exists(sid):
            cp = self.ensure_checkpoint(sid)
            cp.session = session.to_dict()
        else:
            cp = Checkpoint(session=session.to_dict(), messages=self.read_messages(sid))
        self.save_checkpoint(cp)
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

    # 追加真实消息，同时更新工作上下文和只追加的完整历史
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        if self.checkpoints.exists(sid):
            cp = self.ensure_checkpoint(sid)
            cp.messages.append({"role": role, "content": content})
            self.record_message(cp, role, content, run_id)
            self.save_checkpoint(cp)
            return
        # 旧会话尚无快照时也只追加原文
        cp = Checkpoint(session={"id": sid})
        self.record_message(cp, role, content, run_id)
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        (path / "thread.jsonl").touch(exist_ok=True)
        self.flush_history(cp)

    # 兼容旧调用方的批量追加接口
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(sid, str(msg["role"]), msg["content"], run_id)

    # 登记不可变的原始消息，随下一次检查点提交；固定 ID 用于崩溃后补写去重
    def record_message(
        self, cp: Checkpoint, role: str, content: MessageContent, run_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "id": uuid4().hex, "ts": _now(), "role": role, "content": deepcopy(content),
        }
        if run_id is not None:
            row["run_id"] = run_id
        cp.history_pending.append(row)

    # 先原子提交执行状态和待追加原文，再同步完整历史；失败时禁止继续推进
    def save_checkpoint(self, cp: Checkpoint) -> None:
        sid = str(cp.session["id"])
        path = self.session_dir(sid) / "thread.jsonl"
        try:
            if not cp.history_initialized:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.touch()
                    # 兼容旧快照缺少 thread 的情形，只能导出仍然存在的消息
                    if cp.messages and not cp.history_pending:
                        logger.warning("seed missing legacy history from checkpoint sid=%s", sid)
                        for message in cp.messages:
                            self.record_message(cp, message["role"], message["content"])
                cp.history_initialized = True
            elif not path.exists():
                raise CheckpointError(f"full history file is missing for {sid}")
            self.checkpoints.save(cp)
            self.flush_history(cp)
        except OSError as exc:
            raise CheckpointError(f"cannot save full history for {sid}: {exc}") from exc

    # 将已提交原文幂等追加到 thread；不重写旧记录，也不保存摘要和工具预览
    def flush_history(self, cp: Checkpoint) -> None:
        sid = str(cp.session["id"])
        path = self.session_dir(sid) / "thread.jsonl"
        try:
            if not path.exists():
                raise CheckpointError(f"full history file is missing for {sid}")
            if not cp.history_pending:
                return
            rows = self._read_history_rows(sid, repair_tail=True)
            existing = {row["id"]: row for row in rows if "id" in row}
            with path.open("ab") as stream:
                for row in cp.history_pending:
                    previous = existing.get(row["id"])
                    if previous is not None:
                        if previous != row:
                            raise CheckpointError("conflicting history message id")
                        continue
                    stream.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
                    existing[row["id"]] = row
                stream.flush()
                os.fsync(stream.fileno())
            # 磁盘快照仍可能带着这批消息；下次加载按 ID 跳过，下一次保存自然清空
            cp.history_pending.clear()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CheckpointError(f"cannot append full history for {sid}: {exc}") from exc

    # 前端读取完整原文，不做摘要、裁剪或孤立工具调用过滤
    def read_history(self, sid: str, *, include_metadata: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "role": row["role"], "content": row.get("content", ""),
                **({key: row[key] for key in ("id", "run_id") if key in row}
                   if include_metadata else {}),
            }
            for row in self._read_history_rows(sid)
            if row.get("role") in ("user", "assistant")
        ]

    # 游标是 thread 的字节位置；固定 end 后，后续追加留给实时事件，不混进旧快照。
    def read_history_page(
        self, sid: str, cursor: int = 0, history_end: int | None = None, *,
        include_metadata: bool = False,
    ) -> tuple[list[dict[str, Any]], int | None, int]:
        path = self.session_dir(sid) / "thread.jsonl"
        size = path.stat().st_size if path.exists() else 0
        end = size if history_end is None else history_end
        if not 0 <= cursor <= end <= size:
            raise ValueError("invalid history cursor or snapshot end")
        if cursor == end:
            return [], None, end
        messages: list[dict[str, Any]] = []
        consumed, offset = 0, cursor
        with path.open("rb") as stream:
            if cursor:
                stream.seek(cursor - 1)
                if stream.read(1) != b"\n":
                    raise ValueError("history cursor must point to a message boundary")
            stream.seek(cursor)
            while offset < end and len(messages) < _HISTORY_PAGE_MESSAGES:
                line = stream.readline(end - offset)
                # 不拆开一条消息；单条超过页目标时单独返回，由传输层检查硬上限。
                if messages and consumed + len(line) > _HISTORY_PAGE_BYTES:
                    break
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("invalid history row")
                except (ValueError, UnicodeError) as exc:
                    raise CheckpointError(
                        f"invalid history row for {sid} at byte {offset}"
                    ) from exc
                offset = stream.tell()
                consumed += len(line)
                if row.get("role") in ("user", "assistant"):
                    messages.append({
                        "role": row["role"], "content": row.get("content", ""),
                        **({key: row[key] for key in ("id", "run_id") if key in row}
                           if include_metadata else {}),
                    })
        return messages, offset if offset < end else None, end

    # 读取原始行；崩溃留下的未完成末行只在补写时修复，其余损坏明确报错
    def _read_history_rows(self, sid: str, *, repair_tail: bool = False) -> list[dict[str, Any]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []
        try:
            data = path.read_bytes()
            rows: list[dict[str, Any]] = []
            offset = 0
            for line in data.splitlines(keepends=True):
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("invalid history row")
                except (ValueError, UnicodeError) as exc:
                    if repair_tail and offset + len(line) == len(data) and not line.endswith(b"\n"):
                        with path.open("r+b") as stream:
                            stream.truncate(offset)
                        return rows
                    raise CheckpointError(
                        f"invalid history row for {sid} at byte {offset}"
                    ) from exc
                rows.append(row)
                offset += len(line)
            if repair_tail and data and not data.endswith(b"\n"):
                with path.open("ab") as stream:
                    stream.write(b"\n")
            return rows
        except OSError as exc:
            raise CheckpointError(f"cannot read full history for {sid}: {exc}") from exc

    # 模型读取检查点中的工作上下文；旧会话导入时才裁掉孤立工具调用
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        if self.checkpoints.exists(sid):
            return self.checkpoints.load(sid).messages
        from kama_claude.core.compact.budget import truncate_tool_results

        return truncate_tool_results(self._trim_orphan_tool_use(self.read_history(sid)))

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

    # 压缩只替换模型工作上下文，不修改完整对话文件
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        cp = self.ensure_checkpoint(sid)
        cp.messages = messages
        self.save_checkpoint(cp)
