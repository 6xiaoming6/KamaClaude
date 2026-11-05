from __future__ import annotations

import pytest

from kama_claude.core.checkpoint.checkpoint import RunCheckpoint
from kama_claude.core.context import ExecutionContext
from kama_claude.core.memory.model import MemorySnapshot


# 构造只有一份 Run 状态的提示词测试对象
def _context(memory_text="", **kwargs) -> ExecutionContext:
    return ExecutionContext(
        run=RunCheckpoint(run_id="r", goal="g", model="fake", workspace="", max_steps=5,
                          memory_snapshot=MemorySnapshot(text=memory_text)),
        **kwargs,
    )


# 功能：验证统一记忆片段直接接到提示中，不再由 Context 分别读取三层文件
# 设计：传入预先组织的片段，精确检查拼接顺序与工具指引
def test_memory_context_is_injected() -> None:
    prompt = _context(memory_text="\n\nMEMORY").system_prompt("BASE")
    assert prompt.startswith("BASE")
    assert "memory_read" in prompt and "memory_write" in prompt
    assert prompt.endswith("\n\nMEMORY")


# 功能：验证没有任何记忆时仍指导模型使用新工具
# 设计：空 Context 不依赖磁盘文件，覆盖首次使用不能主动记忆的问题
def test_empty_memory_still_has_guidance() -> None:
    prompt = _context().system_prompt("BASE")
    assert "memory_write" in prompt and "user preferences" in prompt


# 功能：验证 Skill 覆盖基础提示时仍保留记忆指引和选定片段
# 设计：显式区分 BASE、SKILL、MEMORY，避免只断言非空而漏掉覆盖顺序
def test_skill_and_memory_are_composed() -> None:
    prompt = _context(system_prompt_override="SKILL", memory_text=" MEMORY").system_prompt("BASE")
    assert prompt.startswith("SKILL") and "BASE" not in prompt
    assert prompt.endswith(" MEMORY")


# 功能：验证冻结提示优先，修改内存记忆片段也不能改变恢复输入
# 设计：冻结后给出不同 base 和 memory，要求返回完全相同的字符串
def test_frozen_prompt_is_returned_verbatim() -> None:
    context = _context(memory_text="changed")
    context.run.system_prompt = "frozen"
    assert context.system_prompt("different base") == "frozen"


# 功能：验证旧的三层输入参数已删除，不提供兼容入口
# 设计：让 Python 构造器拒绝旧字段，防止长期维护两套注入逻辑
def test_old_context_fields_are_removed() -> None:
    with pytest.raises(TypeError):
        _context(session_notes="old")
