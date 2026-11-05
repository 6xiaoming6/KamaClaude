from __future__ import annotations

import pytest

from kama_claude.tui.app import KamaTuiApp
from kama_claude.tui.widgets.permissions import PermissionBlock, PermissionSelect
from tests.unit.test_workspace_tui import Client, idle_socket


# 功能：验证外部编辑审批展示完整目标及新旧片段，并仍只有两个单次选项
# 设计：真实 Textual 打开待审批快照，代码含方括号，排除 Rich 标记误解析
async def test_edit_approval_preview_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KamaTuiApp, "_socket_loop", idle_socket)
    app = KamaTuiApp("127.0.0.1", 1)
    client = Client({
        "type": "permission.requested", "tool_use_id": "edit", "tool_name": "edit_file",
        "workspace": "C:/repo", "resolved_path": "C:/outside/config.py", "outside_workspace": True,
        "params": {"path": "../config.py", "old_text": "items[0] = 30", "new_text": "items[0] = 60"},
        "allowed_decisions": ["allow_once", "deny_once"],
    })
    async with app.run_test(size=(110, 45)) as pilot:
        app._client = client
        await app._open_session("sess-one")
        await pilot.pause()
        content = str(app.query_one(PermissionBlock).render())
        assert "C:/outside/config.py" in content
        assert "items[0] = 30" in content and "items[0] = 60" in content
        assert [item[0] for item in app.query_one(PermissionSelect)._choices] == ["allow_once", "deny_once"]
