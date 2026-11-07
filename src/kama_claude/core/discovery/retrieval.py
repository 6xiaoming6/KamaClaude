from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Literal

from rank_bm25 import BM25Plus  # type: ignore[import-untyped]

from kama_claude.core.config import ToolSearchConfig
from kama_claude.core.discovery.embedding import Embedder, LocalE5Embedder
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.store import CatalogStore

log = logging.getLogger(__name__)


# 保留完整名称，拆分下划线与驼峰；中文以单字和双字组合支持匹配。
def tokenize(text: str) -> list[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    tokens = re.findall(r"[a-zA-Z0-9_]+", expanded.lower())
    tokens += [part for token in tokens.copy() for part in token.split("_") if part != token]
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(word)
        tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
    return tokens


# BM25Plus 的零 delta 避免单条或常见词目录出现负 IDF，同时不奖励未命中文档。
def keyword_ranking(tools: list[CatalogTool], query: str) -> list[tuple[str, float]]:
    terms = tokenize(query)
    if not tools or not terms:
        return []
    corpus = [tokenize(tool.search_text + "\n" + tool.public_name) or [""] for tool in tools]
    index = BM25Plus(corpus, delta=0)
    scores = index.get_scores(terms)
    ranked = [(tool.public_name, float(score)) for tool, score in zip(tools, scores, strict=True)
              if score > 0]
    return sorted(ranked, key=lambda item: (-item[1], item[0]))[:10]


# 不混加不同量纲的原始分数；某路未命中就不贡献分数。
def reciprocal_rank_fusion(rankings: list[list[tuple[str, float]]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (name, _) in enumerate(ranking, 1):
            scores[name] = scores.get(name, 0.0) + 1 / (60 + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


# 同时校验维度和有限数值，损坏缓存会被覆盖而不是传播到排序中。
def normalized(vector: list[float], dimension: int | None = None) -> list[float]:
    if not vector or (dimension is not None and len(vector) != dimension):
        raise ValueError("embedding dimension mismatch")
    if any(not math.isfinite(value) for value in vector):
        raise ValueError("embedding contains invalid numbers")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("embedding has zero norm")
    return [value / norm for value in vector]


@dataclass(frozen=True)
class SearchResult:
    ranking: list[tuple[str, float]]
    method: str
    reason: str = ""


class ToolRetriever:
    # 一个 Core 共用模型和向量缓存；本轮可检索范围由调用者显式传入。
    def __init__(
        self, store: CatalogStore, config: ToolSearchConfig, embedder: Embedder | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._embedder = embedder or LocalE5Embedder(config.model_path)
        self._lock = asyncio.Lock()
        self._worker_lock = threading.Lock()
        self._warned: set[str] = set()
        self.embedding_recomputations = 0

    # 关键词始终可用；模型或缓存出错时只降级搜索，不扩大候选范围。
    async def search(
        self, tools: list[CatalogTool], background: str, description: str, keywords: list[str],
        *, mode: Literal["keyword", "semantic", "hybrid"] = "hybrid",
    ) -> SearchResult:
        background = background[:1000]
        # 两路使用独立输入：关键词不混入背景，向量不混入关键词。
        lexical = keyword_ranking(tools, " ".join(keywords))
        if mode == "keyword":
            return SearchResult(lexical, "keyword_only")
        if not tools:
            return SearchResult([], "keyword_only", "no eligible MCP tools")
        try:
            async with self._lock:
                semantic = await asyncio.to_thread(self._semantic, tools, background, description)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if reason not in self._warned:
                self._warned.add(reason)
                log.warning("tool search degraded to keyword_only: %s", reason)
            return SearchResult(lexical, "keyword_only", reason)
        if mode == "semantic":
            return SearchResult(semantic, "semantic")
        return SearchResult(reciprocal_rank_fusion([lexical, semantic]), "hybrid")

    # 离线评估也复用同一条向量路径，缓存未命中才批量编码。
    def _semantic(
        self, tools: list[CatalogTool], background: str, description: str,
    ) -> list[tuple[str, float]]:
        with self._worker_lock:
            return self._rank_semantic(tools, background, description)

    # 工作锁在线程内持有；等待者取消也不会让另一次推理并发进入同一模型。
    def _rank_semantic(
        self, tools: list[CatalogTool], background: str, description: str,
    ) -> list[tuple[str, float]]:
        key = self._embedder.fingerprint()
        query = normalized(self._embedder.query(background, description))
        vectors: dict[str, list[float]] = {}
        missing: dict[str, CatalogTool] = {}
        for tool in tools:
            vector = self.store.vector(key, tool.search_text_hash)
            if vector is None or len(vector) != len(query):
                missing[tool.search_text_hash] = tool
            else:
                vectors[tool.search_text_hash] = vector
        if missing:
            encoded = self._embedder.documents([tool.search_text for tool in missing.values()])
            if len(encoded) != len(missing):
                raise ValueError("embedding batch size mismatch")
            fresh = {
                text_hash: normalized(vector, len(query))
                for text_hash, vector in zip(missing, encoded, strict=True)
            }
            self.embedding_recomputations += len(fresh)
            self.store.save_vectors(key, fresh)
            vectors.update(fresh)
        scores = [
            (tool.public_name, sum(a * b for a, b in zip(
                query, vectors[tool.search_text_hash], strict=True,
            ))) for tool in tools
        ]
        return sorted((item for item in scores if item[1] > 0),
                      key=lambda item: (-item[1], item[0]))[:10]
