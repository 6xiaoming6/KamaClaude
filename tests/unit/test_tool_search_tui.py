from __future__ import annotations

import json

from textual.widgets import Static

from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.widgets.conversation import ToolCallBlock
from tests.unit.test_tui_recovery import idle_socket


# 功能：TUI 显示搜索描述，展开结果可查看降级方式、加载名称与预算原因。
# 设计：运行真实 Textual 生命周期，使用普通工具事件，不增加专用页面。
async def test_search_result_can_be_viewed_in_tui(monkeypatch):
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    output = json.dumps({"method": "keyword_only", "loaded": ["remote__add"],
                         "not_loaded_budget": ["remote__large"]})
    async with app.run_test(size=(110, 40)) as pilot:
        block = ToolCallBlock("search_tool", {"description": "Find addition tool"})
        app._view.append(block)
        await pilot.pause()
        block.set_result(output, 8)
        block.on_click()
        await pilot.pause()
        summary = str(block.query_one(".summary", Static).render())
        detail = str(block.query_one(".detail", Static).render())
        assert "Find addition tool" in summary
        assert "keyword_only" in detail and "remote__add" in detail and "remote__large" in detail
