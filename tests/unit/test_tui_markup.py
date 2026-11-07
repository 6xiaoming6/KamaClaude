from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from kama_claude.tui.widgets.conversation import LLMStreamBlock, ToolCallBlock


# 真实 CSS 的属性选择器不能被 Textual 当作颜色标记；参数和输出分别覆盖。
@pytest.mark.parametrize("location", ["params", "output"])
async def test_expand_tool_displays_css_as_literal_text(location):
    code = 'button[aria-pressed="true"] { color: red; }\n[dim]literal[/dim]'
    params = {"path": "style.css", "content": code if location == "params" else "body {}"}
    block = ToolCallBlock("write_file", params)

    class Screen(App):
        def compose(self) -> ComposeResult:
            yield block

    async with Screen().run_test() as pilot:
        block.set_result(code if location == "output" else "saved", 12)
        await pilot.pause()
        block.on_click()
        await pilot.pause()
        assert block.has_class("expanded")
        detail = block.query_one(".detail", Static).content
        assert isinstance(detail, Text)
        assert (code if location == "output" else block._params_full) in detail.plain
        assert "[dim]literal[/dim]" in detail.plain
        block.on_click()
        assert not block.has_class("expanded")


async def test_tool_summary_and_stream_tokens_accept_brackets():
    block = ToolCallBlock("bash", {"command": 'echo [aria-pressed="true"]'})
    stream = LLMStreamBlock()

    class Screen(App):
        def compose(self) -> ComposeResult:
            yield block
            yield stream

    async with Screen().run_test() as pilot:
        stream.append_token('CSS: button[aria-pressed="')
        stream.append_token('true"] {} [dim]literal[/dim]')
        await pilot.pause()
        assert '[aria-pressed="true"]' in stream._text
        stream.finalize_markdown()
        block.set_result('items[0] [color="bad"]', 1)
        block.on_click()
        await pilot.pause()
