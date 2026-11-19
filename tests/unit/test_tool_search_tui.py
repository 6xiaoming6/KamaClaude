from __future__ import annotations

import json

from textual.widgets import Static

from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.widgets.conversation import ToolCallBlock
from tests.unit.test_tui_recovery import idle_socket


# 功能：TUI 显示搜索描述，展开结果可查看检索方式与候选概要。
# 设计：运行真实 Textual 生命周期，使用普通工具事件，不增加专用页面。
async def test_search_result_can_be_viewed_in_tui(monkeypatch):
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    output = json.dumps({"method": "keyword_only", "candidates": [
        {"name": "remote__add", "server": "remote", "description": "Add two numbers"},
    ]})
    async with app.run_test(size=(110, 40)) as pilot:
        block = ToolCallBlock("search_tool_candidates", {"description": "Find addition tool"})
        app._view.append(block)
        await pilot.pause()
        block.set_result(output, 8)
        block.on_click()
        await pilot.pause()
        summary = str(block.query_one(".summary", Static).render())
        detail = str(block.query_one(".detail", Static).render())
        assert "Find addition tool" in summary
        assert "keyword_only" in detail and "remote__add" in detail and "Add two numbers" in detail


# 功能：历史加载结果中的结构化引用不显示成 Python 列表。
# 设计：恢复真实历史控件，核对加载参数摘要和展开文本。
async def test_native_search_history_remains_readable(monkeypatch):
    from kama_claude.core.tools.base import ToolResult

    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    output = json.dumps({"loaded": ["remote__add"], "not_loaded_budget": []})
    messages = [
        {"role": "assistant", "run_id": "run-native", "content": [
            {"type": "tool_use", "id": "schema", "name": "get_tool_schema",
             "input": {"names": ["remote__add"]}},
        ]},
        {"role": "user", "run_id": "run-native", "content": [
            {"type": "tool_result", "tool_use_id": "schema",
             "content": ToolResult(output, tool_references=["remote__add"]).message_content()},
        ]},
    ]
    async with app.run_test(size=(110, 40)) as pilot:
        app._view.render_history(messages)
        await pilot.pause()
        block = app._view.query_one(ToolCallBlock)
        block.on_click()
        await pilot.pause()
        detail = str(block.query_one(".detail", Static).render())
        assert "remote__add" in str(block.query_one(".summary", Static).render())
        assert "not_loaded_budget" in detail and "remote__add" in detail
        assert "tool_reference" not in detail and "'type':" not in detail
