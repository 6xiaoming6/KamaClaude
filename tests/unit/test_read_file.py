from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.read_file import ReadFileTool


# 功能：验证读取存在的文件时返回完整内容且 is_error 为 False
# 设计：写临时文件后读取，断言 content 和 is_error，覆盖正常路径（happy path）
async def test_read_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    result = await ReadFileTool(tmp_path).invoke({"path": str(f)})
    assert not result.is_error
    assert result.content.endswith("1 | hello world")
    assert "next_start_line: none" in result.content


# 功能：验证文件不存在时抛出 FileNotFoundError 而非返回错误 ToolResult
# 设计：传入不存在的路径，确认 ReadFileTool 不吞掉异常，让调用方（invoke_tool）负责错误分类和事件发布
async def test_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ReadFileTool(tmp_path).invoke({"path": str(tmp_path / "missing.txt")})


# 功能：验证包含 `..` 的路径被拒绝并抛出 PermissionError
# 设计：传入 `"../secret.txt"` 这种最典型的目录遍历形式，确认安全边界第一道防线有效
async def test_path_traversal_dotdot_raises() -> None:
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "../secret.txt"})


# 功能：验证多级路径中嵌入的 `..` 经过路径规范化后也被正确检测
# 设计：使用 `"subdir/../../etc/passwd"` 测试路径 resolve 后的深度遍历，确认单层 `..` 过滤不足以覆盖此情况
async def test_path_traversal_nested_raises() -> None:
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "subdir/../../etc/passwd"})


# 功能：验证超长行有界读取并标记截断
# 设计：写入 600 KiB 单行，只返回前 2000 字符，避免先读取整个文件再截断
async def test_truncation_over_512kb(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * (600 * 1024))
    result = await ReadFileTool(tmp_path).invoke({"path": str(f)})
    assert not result.is_error
    assert "[line truncated]" in result.content
    assert "1 | " + "x" * 2000 in result.content
    assert len(result.content) < 4096


# 功能：验证恰好 2000 字符的行完整返回
# 设计：检查单行边界值，避免将完整行误标为截断
async def test_exact_line_limit_is_not_truncated(tmp_path: Path) -> None:
    f = tmp_path / "exact.txt"
    f.write_bytes(b"y" * 2000)
    result = await ReadFileTool(tmp_path).invoke({"path": str(f)})
    assert not result.is_error
    assert not result.content.endswith("[truncated]")
    assert result.content.endswith("1 | " + "y" * 2000)


# 功能：验证空文件明确显示空范围而不是失败
# 设计：零字节文件仍带分页元数据，模型可以区分空文件与读取异常
async def test_empty_file_returns_empty_content(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    result = await ReadFileTool(tmp_path).invoke({"path": str(f)})
    assert not result.is_error
    assert "(empty range)" in result.content
    assert "next_start_line: none" in result.content
