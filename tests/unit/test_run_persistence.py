from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kama_claude.core.checkpoint.checkpoint import CheckpointError, RunCheckpoint
from kama_claude.core.checkpoint.persistence import RunPersistence
from kama_claude.core.context import ExecutionContext
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore


# 构造同一 Run 贯穿上下文和持久化的真实存储环境
def _binding(tmp_path: Path) -> RunPersistence:
    store = SessionStore(tmp_path)
    session = Session("sess-one", "chat", "active", "goal", "t", "t")
    store.write_meta(session)
    context = ExecutionContext(
        run=RunCheckpoint(
            run_id="run-one", goal="goal", max_steps=5, model="fake", workspace=str(tmp_path),
        ),
    )
    return RunPersistence(context, store.checkpoints.load(session.id), session, store)


# 功能：验证执行状态没有第二份副本，状态方法直接更新原 Run 并保留中断阶段
# 设计：从外部修改同一 Run，再触发中断和终止，检查各入口看到的状态一致且不依赖同步保存
def test_context_has_one_run_state() -> None:
    run = RunCheckpoint(
        run_id="r", goal="goal", max_steps=5, model="fake", workspace="", phase="tools",
    )
    context = ExecutionContext(run=run)
    run.step = 3
    assert context.run is run and context.run.step == 3
    assert not {"step", "status", "reason", "result", "max_steps"} & vars(context).keys()
    context.mark_interrupted("lost connection")
    assert run.status == "interrupted" and run.phase == "tools"
    context.mark_failed("aborted")
    assert run.status == "failed" and run.phase == "finished"


# 功能：验证保存使用当前 Run 原对象，且继续保留元数据、历史和相同的检查点格式
# 设计：真实写入后重新加载 JSON，确认没有引入新的运行包装字段或改变 schema_version
def test_save_uses_current_run_and_preserves_history_files(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    context = persistence.context
    context.run.step = 2
    context.run.result = "done"
    context.mark_success()
    persistence.save()
    persistence.export_history()
    assert persistence.checkpoint.run is context.run
    restored = persistence.store.checkpoints.load("sess-one")
    assert restored.run.step == 2 and restored.run.status == "success"
    raw = json.loads(persistence.store.checkpoints.path("sess-one").read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "revision", "updated_at", "session", "messages", "run"}
    assert raw["schema_version"] == 1
    folder = persistence.store.session_dir("sess-one")
    assert (folder / "meta.json").exists()
    assert json.loads((folder / "thread.jsonl").read_text(encoding="utf-8"))["content"] == "goal"


# 功能：验证压缩替换 messages 后保存使用新历史，而非之前引用的列表
# 设计：先保存原历史，再替换整个列表并创建新的存储对象读取，覆盖跨次提交与重新加载
def test_save_follows_replaced_working_history(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    persistence.save()
    persistence.context.messages = [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "continue"},
    ]
    persistence.save()
    restored = SessionStore(tmp_path).checkpoints.load("sess-one")
    assert restored.messages == persistence.context.messages
    assert restored.run.run_id == "run-one"


# 功能：验证提交失败直接传播，旧快照与修订号保持已提交版本
# 设计：在原子替换处注入磁盘错误，避免保存对象吞掉错误后让执行继续推进
def test_save_failure_propagates_and_preserves_disk(tmp_path: Path) -> None:
    persistence = _binding(tmp_path)
    persistence.save()
    old = persistence.store.checkpoints.path("sess-one").read_bytes()
    revision = persistence.checkpoint.revision
    persistence.context.run.step = 2
    with patch("kama_claude.core.checkpoint.checkpoint.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(CheckpointError):
            persistence.save()
    assert persistence.store.checkpoints.path("sess-one").read_bytes() == old
    assert persistence.checkpoint.revision == revision
