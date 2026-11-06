from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import astuple
from pathlib import Path
from typing import Any

from kama_claude.core.discovery.model import CatalogTool


class CatalogError(RuntimeError):
    pass


class CatalogStore:
    # 构造不创建文件，未配置 MCP 的普通运行不产生数据库。
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    # 每次操作独立连接，向量后台线程与主线程不共享 SQLite 连接。
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tools (
                server_id TEXT NOT NULL, server_name TEXT NOT NULL, name TEXT NOT NULL,
                public_name TEXT NOT NULL, definition_json TEXT NOT NULL,
                definition_hash TEXT NOT NULL, search_text TEXT NOT NULL,
                search_text_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (server_id, name)
            );
            CREATE TABLE IF NOT EXISTS vectors (
                model_key TEXT NOT NULL, text_hash TEXT NOT NULL, values_json TEXT NOT NULL,
                PRIMARY KEY (model_key, text_hash)
            );
        """)
        return db

    # 只在完整列表拉取成功后调用；相同定义不执行 UPDATE，删除也在同一事务。
    def sync(self, server_id: str, tools: list[CatalogTool]) -> int:
        if any(tool.server_id != server_id for tool in tools):
            raise CatalogError("catalog server identity mismatch")
        if len({tool.name for tool in tools}) != len(tools):
            raise CatalogError("duplicate MCP tool names")
        try:
            with closing(self._connect()) as db, db:
                previous = {row["name"]: row for row in db.execute(
                    "SELECT name, definition_hash, active FROM tools WHERE server_id=?",
                    (server_id,),
                )}
                changed = 0
                for tool in tools:
                    old = previous.pop(tool.name, None)
                    if old and old["definition_hash"] == tool.definition_hash and old["active"]:
                        continue
                    db.execute("""
                        INSERT INTO tools VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(server_id, name) DO UPDATE SET
                            server_name=excluded.server_name, public_name=excluded.public_name,
                            definition_json=excluded.definition_json,
                            definition_hash=excluded.definition_hash,
                            search_text=excluded.search_text,
                            search_text_hash=excluded.search_text_hash,
                            active=1
                    """, astuple(tool))
                    changed += 1
                for name, old in previous.items():
                    if old["active"]:
                        db.execute("UPDATE tools SET active=0 WHERE server_id=? AND name=?",
                                   (server_id, name))
                        changed += 1
                return changed
        except (OSError, sqlite3.Error) as exc:
            raise CatalogError(f"cannot synchronize tool catalog: {exc}") from exc

    # 缓存本身不授予可执行性；调用方还必须持有对应的活跃 MCP 连接。
    def current_hash(self, server_id: str, name: str) -> str | None:
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT definition_hash FROM tools WHERE server_id=? AND name=? AND active=1",
                    (server_id, name),
                ).fetchone()
                return str(row[0]) if row else None
        except (OSError, sqlite3.Error) as exc:
            raise CatalogError(f"cannot read tool catalog: {exc}") from exc

    # 损坏向量视为缓存未命中，随后覆盖重建，不让 NaN 进入相似度计算。
    def vector(self, model_key: str, text_hash: str) -> list[float] | None:
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT values_json FROM vectors WHERE model_key=? AND text_hash=?",
                    (model_key, text_hash),
                ).fetchone()
            if row is None:
                return None
            value: Any = json.loads(row[0])
            if not isinstance(value, list) or not value or any(
                type(x) not in (int, float) or not math.isfinite(x) for x in value
            ):
                return None
            norm = math.hypot(*value)
            return [float(x) / norm for x in value] if norm > 0 and math.isfinite(norm) else None
        except (ValueError, TypeError):
            return None
        except (OSError, sqlite3.Error) as exc:
            raise CatalogError(f"cannot read embedding cache: {exc}") from exc

    # 向量只保存 JSON 数值，模型切换不会覆盖其他模型的缓存。
    def save_vectors(self, model_key: str, vectors: dict[str, list[float]]) -> None:
        try:
            with closing(self._connect()) as db, db:
                db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)", [
                    (model_key, key, json.dumps(value, allow_nan=False))
                    for key, value in vectors.items()
                ])
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise CatalogError(f"cannot save embedding cache: {exc}") from exc
