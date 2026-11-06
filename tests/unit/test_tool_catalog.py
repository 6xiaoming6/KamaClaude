from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.config import McpServerConfig, ToolSearchConfig
from kama_claude.core.discovery.model import CatalogTool, canonical, server_identity
from kama_claude.core.discovery.store import CatalogError, CatalogStore
from kama_claude.core.mcp.server import McpServerManager
from tests.unit.discovery_support import mcp_client, record


# 功能：字段重排不更新记录，参数默认值变化仍更新完整定义。
# 设计：检查同步计数和独立检索哈希，区分 Schema 缓存与向量缓存。
def test_canonical_hash_and_noop_sync(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    first = record()
    assert store.sync("server-id", [first]) == 1
    reordered = CatalogTool.from_definition("server-id", "github", dict(reversed(
        list(json.loads(first.definition_json).items()),
    )))
    assert reordered == first
    assert store.sync("server-id", [reordered]) == 0
    changed = record(default=2)
    assert changed.definition_hash != first.definition_hash
    assert changed.search_text_hash == first.search_text_hash
    assert store.sync("server-id", [changed]) == 1
    assert store.current_hash("server-id", "get_issue") == changed.definition_hash


# 功能：成功获取空列表才使工具失效，不删除可复用向量。
# 设计：两份服务同名工具隔离，避免刷新一个服务误伤另一个。
def test_removal_and_server_isolation(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    first = record()
    other = CatalogTool.from_definition("other", "second", json.loads(first.definition_json))
    store.sync(first.server_id, [first])
    store.sync(other.server_id, [other])
    store.save_vectors("model", {first.search_text_hash: [1.0, 0.0]})
    assert store.sync(first.server_id, []) == 1
    assert store.current_hash(first.server_id, first.name) is None
    assert store.current_hash(other.server_id, other.name) == other.definition_hash
    assert store.vector("model", first.search_text_hash) == [1.0, 0.0]
    assert store.sync(first.server_id, []) == 0


# 功能：某条工具写入失败时，之前的更新也必须回滚。
# 设计：SQLite 触发器主动制造真实事务错误，而非只断言调用过 rollback。
def test_catalog_sync_is_atomic(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    first = record()
    store.sync(first.server_id, [first])
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER fail_new BEFORE INSERT ON tools
                      WHEN NEW.name='broken' BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
    with pytest.raises(CatalogError, match="disk failure"):
        store.sync(first.server_id, [record(default=2), record("broken")])
    assert store.current_hash(first.server_id, first.name) == first.definition_hash
    assert store.current_hash(first.server_id, "broken") is None


# 功能：重复工具名、跨服务记录不允许提交，数组顺序继续参与定义哈希。
# 设计：在数据库写入前拒绝歧义数据，避免最后一条覆盖前一条。
def test_invalid_catalog_and_array_order(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    with pytest.raises(CatalogError, match="duplicate"):
        store.sync("server-id", [record(), record()])
    with pytest.raises(CatalogError, match="identity"):
        store.sync("different", [record()])
    assert canonical({"enum": [1, 2]}) != canonical({"enum": [2, 1]})


# 功能：同名服务的不同端点和启动目录使用不同目录命名空间。
# 设计：配置中带密钥时只返回摘要，不把请求头拼入可读数据库字段。
def test_server_identity_tracks_endpoint_and_workspace():
    first = McpServerConfig("server", "streamable-http", url="https://one.example/mcp")
    second = McpServerConfig("server", "streamable-http", url="https://two.example/mcp")
    assert server_identity(first, "a") != server_identity(second, "a")
    assert server_identity(first, "a") != server_identity(first, "b")
    assert len(server_identity(first, "a")) == 64


# 功能：重连发现失败或数据库提交失败不发布半份工具，不误删原缓存。
# 设计：先完成真实同步，再让新连接分别失败，保留数据库中的旧定义。
@pytest.mark.parametrize("failure", ["list", "database"])
async def test_failed_reconnect_preserves_cache_and_closes_client(tmp_path, monkeypatch, failure):
    manager = McpServerManager(ToolSearchConfig(db_path=str(tmp_path / "catalog.db")))
    first, second = mcp_client(), mcp_client()
    monkeypatch.setattr(manager, "_connect", AsyncMock(side_effect=[first, second]))
    config = McpServerConfig("remote", command="python")
    await manager.start_all([config])
    old = manager.get_tools()[0].record
    if failure == "list":
        second.list_tools.side_effect = RuntimeError("page 2 failed")
    else:
        monkeypatch.setattr(manager.catalog, "sync", lambda *args: (_ for _ in ()).throw(
            CatalogError("cannot commit"),
        ))
    await manager.start_all([config])
    assert manager.get_tools() == []
    assert manager.catalog.current_hash(old.server_id, old.name) == old.definition_hash
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


# 功能：原工具在重连后定义变动时不能静默执行新版本。
# 设计：保留旧工具对象再更新目录，确认拒绝发生在远程调用之前。
async def test_loaded_tool_rejects_changed_definition(tmp_path, monkeypatch):
    manager = McpServerManager(ToolSearchConfig(db_path=str(tmp_path / "catalog.db")))
    client = mcp_client()
    monkeypatch.setattr(manager, "_connect", AsyncMock(return_value=client))
    await manager.start_all([McpServerConfig("remote", command="python")])
    tool = manager.get_tools()[0]
    changed = json.loads(tool.record.definition_json)
    changed["description"] = "changed capability"
    manager.catalog.sync(tool.record.server_id, [CatalogTool.from_definition(
        tool.record.server_id, "remote", changed,
    )])
    result = await tool.invoke({"number": 1})
    assert result.is_error and result.error_type == "schema_error" and not result.outcome_unknown
    client.call_tool.assert_not_awaited()
    await manager.stop_all()


# 功能：服务前缀与原工具名拼接碰撞时，不允许后注册工具覆盖实际执行目标。
# 设计：用两个不同服务构造相同公开名，确认第二批不提交、不发布。
async def test_public_name_collision_is_rejected(tmp_path, monkeypatch):
    from kama_claude.core.mcp.client import McpToolDef
    manager = McpServerManager(ToolSearchConfig(db_path=str(tmp_path / "catalog.db")))
    first, second = mcp_client(), mcp_client()
    first.list_tools.return_value = [McpToolDef("b__c", "first")]
    second.list_tools.return_value = [McpToolDef("c", "second")]
    monkeypatch.setattr(manager, "_connect", AsyncMock(side_effect=[first, second]))
    await manager.start_all([McpServerConfig("a", command="python"),
                             McpServerConfig("a__b", command="python")])
    assert [tool.name for tool in manager.get_tools()] == ["a__b__c"]
    assert manager.get_tools()[0].server_name == "a"
    second.close.assert_awaited_once()
    await manager.stop_all()
