from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kama_claude.core.config import KamaConfig, ToolSearchConfig
from kama_claude.core.config.environment import apply_env
from kama_claude.core.config.toml import apply_toml
from kama_claude.core.discovery import embedding as embedding_module
from kama_claude.core.discovery.embedding import LocalE5Embedder
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.retrieval import (
    ToolRetriever,
    keyword_ranking,
    reciprocal_rank_fusion,
    tokenize,
)
from kama_claude.core.discovery.store import CatalogStore
from tests.unit.discovery_support import FakeEmbedder, record


# 功能：关键词支持完整名称、驼峰、参数名和中文，单工具目录也有正分。
# 设计：覆盖 BM25 常见词和单文档 IDF 的边界，不只测试英文大目录。
def test_keyword_fields_and_small_catalog():
    assert {"get_issue", "get", "issue"} <= set(tokenize("get_issue"))
    assert {"get", "issue"} <= set(tokenize("getIssue"))
    assert "数据库"[:2] in tokenize("数据库")
    tool = record(description="读取数据库表结构")
    for query in ["get_issue", "number", "数据库"]:
        assert keyword_ranking([tool], query)[0][0] == tool.public_name
    assert keyword_ranking([tool], "completely_unknown") == []


# 功能：两路排名按 RRF 融合，不混加原始相似度分数。
# 设计：让一路分数极大，仍由双路共同靠前的工具获胜。
def test_rrf_ranking_and_deduplication():
    ranking = reciprocal_rank_fusion([[('a', 999999), ('b', 1)], [('b', 0.9), ('c', 0.8)]])
    assert [name for name, _ in ranking] == ["b", "a", "c"]
    assert ranking[0][1] == pytest.approx(1 / 62 + 1 / 61)


# 功能：向量仅为缺失或变化文本生成，模型版本与缓存损坏会触发重建。
# 设计：记录真实编码条数，通过 SQLite 写入坏数据验证失效路径。
async def test_embedding_cache_invalidation(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    embedding = FakeEmbedder()
    retriever = ToolRetriever(store, ToolSearchConfig(), embedding)
    tool = record()
    assert (await retriever.search([tool], "task", "缺陷详情")).method == "hybrid"
    assert embedding.encoded == 1
    await retriever.search([tool], "task", "缺陷详情")
    await retriever.search([record(default=2)], "task", "缺陷详情")
    assert embedding.encoded == 1
    changed = record(description="Get issue details and comments")
    await retriever.search([changed], "task", "缺陷详情")
    assert embedding.encoded == 2
    embedding.key = "fake-v2"
    await retriever.search([tool], "task", "缺陷详情")
    assert embedding.encoded == 3
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE vectors SET values_json='[NaN]' WHERE model_key='fake-v2'")
    await retriever.search([tool], "task", "缺陷详情")
    assert embedding.encoded == 4


# 功能：模型、维度或数据库出错明确降级关键词，不误称混合检索。
# 设计：注入错误到不同边界，要求仍返回可解释的关键词结果。
@pytest.mark.parametrize("failure", ["model", "dimension", "database"])
async def test_semantic_failures_fall_back(tmp_path, monkeypatch, failure):
    store = CatalogStore(tmp_path / "tools.db")
    embedding = FakeEmbedder()
    if failure == "model":
        embedding.fingerprint = Mock(side_effect=RuntimeError("missing model"))
    elif failure == "dimension":
        embedding.documents = Mock(return_value=[[1.0]])
    else:
        monkeypatch.setattr(store, "save_vectors", Mock(side_effect=RuntimeError("disk full")))
    result = await ToolRetriever(store, ToolSearchConfig(), embedding).search(
        [record()], "task", "issue",
    )
    assert result.method == "keyword_only" and result.reason
    assert result.ranking[0][0] == "github__get_issue"


# 功能：未获准工具即使语义高度匹配也不会进入检索候选。
# 设计：检索器只接受调用方提供的目录，测试没有从数据库补出隐藏工具。
async def test_filtered_catalog_and_query_context(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    hidden = record()
    allowed = CatalogTool.from_definition("server", "db", {
        "name": "database", "description": "Query SQL database", "inputSchema": {},
    })
    store.sync(hidden.server_id, [hidden])
    embedding = FakeEmbedder()
    result = await ToolRetriever(store, ToolSearchConfig(), embedding).search(
        [allowed], "x" * 2000, "缺陷",
    )
    assert result.ranking == []
    assert len(embedding.queries[0][0]) == 1000
    assert embedding.queries[0][1] == "缺陷"


# 功能：本地目录不存在时绝不触发 Hugging Face 下载。
# 设计：使用真实离线加载入口，无需安装推理框架也能验证目录校验。
def test_local_model_missing_is_explicit(tmp_path):
    with pytest.raises(RuntimeError, match="missing"):
        embedding_module.model_directory(str(tmp_path / "missing"))


# 功能：配置覆盖与直接构造使用同一校验边界。
# 设计：先读 TOML，再用环境覆盖，拒绝超出 Top-K 和非法布尔值。
def test_tool_search_config(monkeypatch):
    config = KamaConfig()
    apply_toml(config, {"tool_search": {"model_path": "local/e5", "top_k": 2}})
    monkeypatch.setenv("KAMA_TOOL_SEARCH_TOP_K", "5")
    monkeypatch.setenv("KAMA_TOOL_SEARCH_ENABLED", "false")
    apply_env(config)
    assert config.tool_search.top_k == 5 and not config.tool_search.enabled
    assert config.tool_search.model_path == "local/e5"
    monkeypatch.setenv("KAMA_TOOL_SEARCH_ENABLED", "maybe")
    with pytest.raises(SystemExit):
        apply_env(config)
    with pytest.raises(SystemExit):
        apply_toml(KamaConfig(), {"tool_search": {"top_k": 6}})


# 功能：模型与推理依赖已由用户准备时，验证真实离线 E5 可生成有限向量。
# 设计：缺少显式本地路径就跳过，禁止为了测试自动下载模型。
@pytest.mark.skipif(not os.environ.get("KAMA_TEST_E5_MODEL_PATH"), reason="local E5 model not provided")
async def test_real_local_e5_smoke(tmp_path):
    embedder = LocalE5Embedder(os.environ["KAMA_TEST_E5_MODEL_PATH"])
    retriever = ToolRetriever(CatalogStore(tmp_path / "tools.db"), ToolSearchConfig(), embedder)
    result = await retriever.search([record()], "查看 GitHub 问题", "获取 issue 详情")
    assert result.method == "hybrid", result.reason
    assert result.ranking[0][0] == "github__get_issue"
    assert retriever.embedding_recomputations == 1
    await retriever.search([record()], "查看 GitHub 问题", "获取 issue 详情")
    assert retriever.embedding_recomputations == 1
    # 模拟模型实例重建，已有文本向量继续从 SQLite 读取。
    restored = ToolRetriever(retriever.store, ToolSearchConfig(), LocalE5Embedder(
        os.environ["KAMA_TEST_E5_MODEL_PATH"],
    ))
    assert (await restored.search([record()], "查看 GitHub 问题", "获取 issue 详情")).method == "hybrid"
    assert restored.embedding_recomputations == 0


# 功能：离线适配明确指定 CPU、禁止远程代码，并为背景和需求保留 Token 空间。
# 设计：替换模型库边界，验证真实适配代码的参数和输入，不下载真实权重。
def test_local_e5_adapter_uses_offline_parameters_and_prefixes(tmp_path, monkeypatch):
    (tmp_path / "model.safetensors").write_bytes(b"local test weights")
    calls = []
    tokenizer = SimpleNamespace(encode=lambda text, **kwargs: list(text), decode=lambda ids: "".join(ids))

    # 模拟模型对象，仅捕获适配器实际传入的文本与加载选项。
    class Model:
        # 记录模型加载参数，提供确定性的测试分词器。
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))
            self.tokenizer = tokenizer

        # 不模拟语义，仅返回有限向量以检查输入预处理。
        def encode(self, texts, **kwargs):
            calls.append(texts)
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(embedding_module.importlib, "import_module",
                        lambda name: SimpleNamespace(SentenceTransformer=Model))
    embedder = LocalE5Embedder(str(tmp_path))
    first_key = embedder.fingerprint()
    assert calls[0][1] == {"device": "cpu", "local_files_only": True, "trust_remote_code": False}
    embedder.documents(["tool information"])
    assert calls[-1] == ["passage: tool information"]
    embedder.query("B" * 1000, "CURRENT_NEED" + "D" * 1000)
    query = calls[-1][0]
    assert query.startswith("query: ") and "CURRENT_NEED" in query
    assert query.count("B") == 128 and len(query) <= 508
    (tmp_path / "model.safetensors").write_bytes(b"changed weights")
    assert LocalE5Embedder(str(tmp_path)).fingerprint() != first_key


# 功能：搜索等待者取消后，后续搜索不能并发进入同一个模型。
# 设计：第一条查询阻塞真实工作线程，取消协程后确认第二条仍等待工作锁。
async def test_cancelled_waiter_keeps_model_serialized(tmp_path):
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    embedding = FakeEmbedder()
    original = embedding.query

    # 只有第一条请求阻塞，其余请求记录进入时间。
    def query(background, description):
        if description == "first":
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return original(background, description)

    embedding.query = query
    retriever = ToolRetriever(CatalogStore(tmp_path / "tools.db"), ToolSearchConfig(), embedding)
    first = asyncio.create_task(retriever.search([record()], "task", "first"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(retriever.search([record()], "task", "second"))
        await asyncio.sleep(0.05)
        assert not second_entered.is_set()
    finally:
        release.set()
    assert (await asyncio.wait_for(second, 3)).method == "hybrid"


# 功能：评估脚本三种模式使用相同样本，正确报告真实编码次数和召回指标。
# 设计：使用标注测试向量验证指标计算，明确不代表真实 E5 效果。
async def test_evaluation_modes_and_metrics(tmp_path):
    from scripts.eval_tool_search import evaluate
    data = json.loads((Path(__file__).parents[1] / "fixtures/tool_search_eval.json").read_text(encoding="utf-8"))
    tools = [CatalogTool.from_definition("eval", "eval", item) for item in data["tools"]]
    for mode in ["keyword", "semantic", "hybrid"]:
        result = await evaluate(tools, data["queries"], CatalogStore(tmp_path / f"{mode}.db"),
                                FakeEmbedder(), mode)
        assert result["status"] == "ok" and result["queries"] == 8
        assert 0 <= result["recall_at_3"] <= 1
        assert result["mean_schema_estimated_tokens"] >= 0
        assert result["embedding_recomputations"] == (0 if mode == "keyword" else len(tools))


# 功能：完整默认缓存不联网，缓存不完整才请求下载必要权重。
# 设计：替换 Hugging Face 边界，不在普通测试中下载几百 MB 的模型。
@pytest.mark.parametrize("complete", [True, False])
def test_default_model_uses_cache_before_download(tmp_path, monkeypatch, complete):
    files = ["model.safetensors", "config.json", "modules.json", "1_Pooling/config.json",
             "tokenizer.json", "tokenizer_config.json", "sentencepiece.bpe.model"]
    if complete:
        for name in files:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    snapshot = Mock(return_value=str(tmp_path))
    monkeypatch.setattr(embedding_module.importlib, "import_module",
                        lambda name: SimpleNamespace(snapshot_download=snapshot))
    assert embedding_module.model_directory("") == tmp_path
    assert snapshot.call_count == (1 if complete else 2)
    assert snapshot.call_args_list[0].kwargs["local_files_only"] is True
    if not complete:
        kwargs = snapshot.call_args.kwargs
        assert kwargs["local_files_only"] is False
        assert "model.safetensors" in kwargs["allow_patterns"]
        assert "onnx/*" in kwargs["ignore_patterns"]
