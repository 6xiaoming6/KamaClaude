from __future__ import annotations

from kama_claude.core.compact.budget import truncate_tool_results


def _make_tool_result_msg(content: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "id1", "content": content}],
    }


# 功能：验证 tool_result 内容未超过阈值时原文不变
# 设计：构造 7999 字符内容（刚好低于 8000），断言消息原样返回
def test_short_tool_result_untouched() -> None:
    text = "x" * 7999
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["content"] == text


# 功能：验证 tool_result 内容超过阈值时被截断并附加省略标记
# 设计：构造 10000 字符内容，断言截断后长度 < 原始，且包含省略标记字符串
def test_long_tool_result_truncated() -> None:
    text = "y" * 10_000
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    truncated = result[0]["content"][0]["content"]
    assert len(truncated) < len(text)
    assert "chars omitted" in truncated
    assert truncated.startswith("y" * 2000)
    assert truncated.endswith("y" * 2000)


# 功能：验证 tool_result 内容恰好等于阈值时不截断
# 设计：构造恰好 8000 字符内容，断言原文保持不变
def test_exact_limit_untouched() -> None:
    text = "z" * 8000
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["content"] == text


# 功能：验证 text 类型 block 不受截断影响
# 设计：构造含 text block 的 user 消息，内容超过阈值，断言内容原样返回
def test_non_tool_result_block_untouched() -> None:
    long_text = "a" * 20_000
    msgs = [{"role": "user", "content": [{"type": "text", "text": long_text}]}]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["text"] == long_text


# 功能：验证同一 user 消息含多个 tool_result 时各自独立判断截断
# 设计：构造一条消息含两个 tool_result，一短一长，断言只有长的被截断
def test_multiple_tool_results_independent() -> None:
    short = "s" * 100
    long = "l" * 10_000
    msgs = [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": short},
            {"type": "tool_result", "tool_use_id": "b", "content": long},
        ],
    }]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    blocks = result[0]["content"]
    assert blocks[0]["content"] == short
    assert "chars omitted" in blocks[1]["content"]


# 功能：验证 assistant 消息不被截断处理
# 设计：构造超长内容的 assistant 消息，断言原样返回
def test_assistant_message_untouched() -> None:
    text = "a" * 20_000
    msgs = [{"role": "assistant", "content": text}]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"] == text


# 功能：验证中文保守估算，以及系统提示和工具定义都会增加请求预算
# 设计：比较相同字符数量的中英文，并分别增加两类固定输入
def test_estimate_counts_unicode_system_and_schemas() -> None:
    from kama_claude.core.compact.budget import estimate_request, estimate_tokens
    assert estimate_tokens("中" * 400) > estimate_tokens("x" * 400) * 3
    base = estimate_request("", [], [])
    assert estimate_request("约定" * 400, [], []) > base + 700
    assert estimate_request("", [], [{"description": "工具" * 400}]) > base + 700


# 功能：验证全文归档、头尾预览及重复整理不覆盖原文
# 设计：使用含回车换行和结尾错误信息的输出，并让预览本身超过小阈值
def test_archive_is_lossless_and_idempotent(tmp_path) -> None:
    text = "HEAD\r\n" + "中" * 1_000 + "\r\nERROR AT END"
    original = [_make_tool_result_msg(text)]
    first = truncate_tool_results(original, limit=200, keep=100, outputs_dir=tmp_path)
    preview = first[0]["content"][0]["content"]
    assert preview.startswith("HEAD") and preview.endswith("ERROR AT END")
    archived = list(tmp_path.glob("*.txt"))
    assert len(archived) == 1 and archived[0].read_bytes() == text.encode("utf-8")
    assert str(archived[0]) in preview
    assert truncate_tool_results(first, limit=200, keep=100, outputs_dir=tmp_path) == first
    assert archived[0].read_bytes() == text.encode("utf-8")
    assert original[0]["content"][0]["content"] == text


# 功能：验证归档原子替换失败时不返回裁剪历史，且临时文件得到清理
# 设计：只阻断最终替换操作，覆盖全文已写入但尚未成为可引用文件的窗口
def test_archive_replace_failure_keeps_original(tmp_path, monkeypatch) -> None:
    import pytest

    from kama_claude.core.compact import budget

    # 模拟磁盘或文件系统替换失败
    def fail_replace(*args) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(budget.os, "replace", fail_replace)
    messages = [_make_tool_result_msg("x" * 10_000)]
    with pytest.raises(OSError):
        truncate_tool_results(messages, outputs_dir=tmp_path)
    assert messages[0]["content"][0]["content"] == "x" * 10_000
    assert not list(tmp_path.iterdir())


# 功能：验证工具读取带真实预览的文档时，不能把整份新输出误判成旧预览
# 设计：先产生一个归档预览，再用另一个调用 ID 返回同样文字，核对新全文仍被归档
def test_preview_marker_cannot_hide_a_different_tool_output(tmp_path) -> None:
    first = truncate_tool_results(
        [_make_tool_result_msg("x" * 1_000)], limit=200, keep=100, outputs_dir=tmp_path / "outputs",
    )
    preview = first[0]["content"][0]["content"]
    copied = [_make_tool_result_msg(preview)]
    copied[0]["content"][0]["tool_use_id"] = "different-call"
    second = truncate_tool_results(copied, limit=200, keep=100, outputs_dir=tmp_path / "outputs")
    assert len(list((tmp_path / "outputs").glob("*.txt"))) == 2
    assert second[0]["content"][0]["content"] != preview


# 功能：验证归档全文丢失时明确停止，不把预览再次冒充完整输出
# 设计：删除只属于测试的归档文件后重复整理，检查没有生成替代的伪全文
def test_missing_archive_is_reported(tmp_path) -> None:
    import pytest
    first = truncate_tool_results(
        [_make_tool_result_msg("x" * 1_000)], limit=200, keep=100, outputs_dir=tmp_path / "outputs",
    )
    archive = next((tmp_path / "outputs").glob("*.txt"))
    archive.unlink()
    with pytest.raises(FileNotFoundError):
        truncate_tool_results(first, limit=200, keep=100, outputs_dir=tmp_path / "outputs")
    assert not list((tmp_path / "outputs").iterdir())
