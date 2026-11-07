from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.config import ToolSearchConfig
from kama_claude.core.discovery.embedding import Embedder, LocalE5Embedder
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.discovery.store import CatalogStore


# 三种模式共用样本、Top-K 和向量实现；每种模式独立冷缓存。
async def evaluate(
    tools: list[CatalogTool], queries: list[dict[str, Any]], store: CatalogStore,
    embedder: Embedder, mode: Literal["keyword", "semantic", "hybrid"],
) -> dict[str, Any]:
    retriever = ToolRetriever(store, ToolSearchConfig(), embedder)
    schemas = {tool.public_name: tool.schema for tool in tools}
    recall, schema_tokens, reasons = [], [], []
    started = time.perf_counter()
    for query in queries:
        result = await retriever.search(
            tools, query["background"], query["description"], query["keywords"], mode=mode,
        )
        if mode != "keyword" and result.method == "keyword_only":
            reasons.append(result.reason)
            continue
        selected = {name for name, _ in result.ranking[:3]}
        expected = set(query["expected"])
        recall.append(len(selected & expected) / len(expected))
        schema_tokens.append(estimate_bytes([schemas[name] for name in sorted(selected)]))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "status": "unavailable" if reasons else "ok",
        "reason": sorted(set(reasons)), "queries": len(queries),
        "recall_at_3": sum(recall) / len(recall) if recall else None,
        "mean_schema_estimated_tokens": sum(schema_tokens) / len(schema_tokens)
        if schema_tokens else None,
        "mean_search_ms": elapsed_ms / max(1, len(queries)),
        "embedding_recomputations": retriever.embedding_recomputations,
    }


# 默认使用库管理的官方模型缓存，也可以显式指定离线目录。
async def main() -> None:
    parser = argparse.ArgumentParser(description="Offline MCP tool retrieval evaluation")
    parser.add_argument("--model-path", default="")
    parser.add_argument("--dataset", type=Path,
                        default=Path(__file__).resolve().parents[1] / "tests/fixtures/tool_search_eval.json")
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    tools = [CatalogTool.from_definition("eval", "eval", tool) for tool in data["tools"]]
    output: dict[str, Any] = {
        "tool_count": len(tools), "full_schema_estimated_tokens": estimate_bytes(
            [tool.schema for tool in tools],
        ), "model_path_configured": bool(args.model_path), "modes": {},
    }
    # 所有临时数据库放在当前仓库缓存目录，结束后只清理新建的独立临时子目录。
    cache = Path(".pytest_cache").resolve()
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tool-eval-", dir=cache) as temporary:
        for mode in ("keyword", "semantic", "hybrid"):
            output["modes"][mode] = await evaluate(
                tools, data["queries"], CatalogStore(Path(temporary) / f"{mode}.db"),
                LocalE5Embedder(args.model_path), mode,
            )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
