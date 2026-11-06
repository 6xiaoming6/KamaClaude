from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from kama_claude.core.subagent.result import SubagentResult
from kama_claude.core.tools.errors import ToolError

_RESULT = TypeAdapter(SubagentResult)


class SubagentStoreError(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "storage_error")


class SubagentResultStore:
    # 只保存已完成结果；不保存协程、不恢复未完成子任务，也不从模型接收文件路径。
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, run_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id) is None:
            raise SubagentStoreError("invalid subagent Run ID")
        path = self.root / run_id / "result.json"
        try:
            if not path.resolve().is_relative_to(self.root):
                raise SubagentStoreError("subagent result path escapes its Run directory")
            if path.parent.is_symlink() or path.parent.is_junction() or path.is_symlink():
                raise SubagentStoreError("subagent result links are not supported")
        except (OSError, RuntimeError) as exc:
            raise SubagentStoreError(f"cannot resolve subagent result: {exc}") from exc
        return path

    def save(self, result: SubagentResult) -> None:
        temporary: Path | None = None
        try:
            path = self._path(result.run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"schema_version": 1, "result": asdict(result)}
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".result-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, UnicodeError) as exc:
            raise SubagentStoreError(f"cannot save subagent result: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self, run_id: str, session_id: str) -> SubagentResult | None:
        path = self._path(run_id)
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
                raise ValueError("missing result version")
            if payload["schema_version"] != 1:
                raise ValueError("unsupported result version")
            result = _RESULT.validate_json(json.dumps(payload["result"]), strict=True)
            if (
                min(result.actual_tokens, result.estimated_tokens,
                    result.child_actual_tokens, result.child_estimated_tokens,
                    result.elapsed_seconds) < 0
                or result.child_actual_tokens > result.actual_tokens
                or result.child_estimated_tokens > result.estimated_tokens
                or len(result.text) > 4_000
                or not math.isfinite(result.elapsed_seconds)
            ):
                raise ValueError("invalid subagent result usage or summary")
            if result.run_id != run_id:
                raise ValueError("subagent result identity mismatch")
            if result.session_id != session_id:
                return None
            return result
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise SubagentStoreError(f"cannot load subagent result {run_id}: {exc}") from exc
