"""Full ToolRet corpus, original queries, frozen KamaClaude E5/BM25/RRF semantics.

An accelerated evaluator (indexed BM25 and batched cosine) is used instead of
rebuilding the index for every query. No data labels enter query generation.
Scores describe this explicitly documented adapter, not an official leaderboard run.
"""
# ruff: noqa: I001 -- initialize existing provider package before discovery imports
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from rank_bm25 import BM25Plus

from kama_claude.core.config import ToolSearchConfig
from kama_claude.core.llm import provider as _provider  # noqa: F401
from kama_claude.core.discovery.embedding import LocalE5Embedder
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.retrieval import keyword_ranking, reciprocal_rank_fusion, tokenize

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "runs/mcp-tool-routing-20261116"


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(ranked: list[str], labels: dict[str, int]) -> dict[str, float]:
    gold = {name for name, rel in labels.items() if rel > 0}
    if not gold:
        raise ValueError("Query has no positive labels")
    result = {}
    positive_rel = sorted((rel for rel in labels.values() if rel > 0), reverse=True)
    for k in (1, 3, 5, 10):
        selected = ranked[:k]
        hits = len(set(selected) & gold)
        dcg = sum(max(labels.get(name, 0), 0)/math.log2(i+2) for i, name in enumerate(selected))
        ideal = sum(rel/math.log2(i+2) for i, rel in enumerate(positive_rel[:k]))
        result.update({f"recall_at_{k}": hits/len(gold), f"hit_at_{k}": float(hits > 0),
                       f"complete_at_{k}": float(gold.issubset(selected)),
                       f"ndcg_at_{k}": dcg/ideal if ideal else 0})
    rank = next((i+1 for i, name in enumerate(ranked[:10]) if name in gold), None)
    result["mrr_at_10"] = 1/rank if rank else 0
    return result


def top(scores: np.ndarray, names: list[str], k: int = 10) -> list[tuple[str, float]]:
    # Preserve positive cutoff and deterministic name tie breaking, including boundary ties.
    positive = np.flatnonzero(scores > 0)
    if len(positive) > k:
        threshold = np.partition(scores[positive], -k)[-k]
        positive = positive[scores[positive] >= threshold]
    return sorted(((names[i], float(scores[i])) for i in positive), key=lambda x: (-x[1], x[0]))[:k]


class FrozenBM25:
    def __init__(self, records: list[CatalogTool]):
        self.names = [record.public_name for record in records]
        tokenized = [tokenize(record.search_text + "\n" + record.public_name) or [""]
                     for record in records]
        self.index = BM25Plus(tokenized, delta=0)
        postings: dict[str, list] = defaultdict(list)
        for i, counts in enumerate(self.index.doc_freqs):
            for word, count in counts.items():
                postings[word].append((i, count))
        self.postings = {word: (np.asarray([i for i, _ in pairs], dtype=np.int32),
                               np.asarray([count for _, count in pairs], dtype=np.float64))
                         for word, pairs in postings.items()}
        lengths = np.asarray(self.index.doc_len, dtype=np.float64)
        self.denominator = self.index.k1*(1-self.index.b+self.index.b*lengths/self.index.avgdl)

    def search(self, query: str) -> list[tuple[str, float]]:
        scores = np.zeros(len(self.names), dtype=np.float64)
        for word in tokenize(query):
            if word not in self.postings:
                continue
            indices, counts = self.postings[word]
            scores[indices] += self.index.idf[word]*counts*(self.index.k1+1)/(self.denominator[indices]+counts)
        return top(scores, self.names)


def self_check(records: list[CatalogTool] | None = None) -> dict:
    assert metrics(["a", "c", "b"], {"a": 1, "b": 1})["recall_at_1"] == 0.5
    assert metrics(["a", "c", "b"], {"a": 1, "b": 1})["complete_at_3"] == 1
    assert metrics([], {"a": 1})["mrr_at_10"] == 0
    assert metrics(["a", "b"], {"a": 1, "b": 1})["ndcg_at_3"] == 1
    assert top(np.array([1., 1., 1.]), ["c", "a", "b"], 2) == [("a", 1.), ("b", 1.)]
    if records is None:
        records = [CatalogTool.from_definition("test", "test", {
            "name": f"tool{i}", "description": text,
        }) for i, text in enumerate(("read file", "write file", "send mail", "read email"))]
    subset = records[:301]
    index = FrozenBM25(subset)
    terms = ["read file", "send mail", "query database", "zzzznonexistent", "tool tool",
             "搜索文件", "model image api", "the", "name"]
    for query in terms:
        original = keyword_ranking(subset, query)
        optimized = index.search(query)
        assert [n for n, _ in original] == [n for n, _ in optimized], query
        assert np.allclose([s for _, s in original], [s for _, s in optimized], rtol=1e-12)
    return {"metrics_known_cases": "passed", "tie_breaking": "passed",
            "indexed_bm25_vs_production": {"queries": len(terms), "tools": len(subset), "passed": True}}


def query_texts(embedder: LocalE5Embedder, queries: list[dict]) -> list[str]:
    # Same preparation as LocalE5Embedder.query('', description), with empty background.
    tokenizer = embedder._model.tokenizer
    prefix = "query: 任务背景：\n当前需求："
    overhead = len(tokenizer.encode(prefix, add_special_tokens=False))
    texts = []
    for query in queries:
        ids = tokenizer.encode(query["query"], add_special_tokens=False)[:max(1, 508-overhead)]
        texts.append(prefix + tokenizer.decode(ids))
    return texts


def mean_metrics(rows: list[dict]) -> dict:
    output = {}
    for mode in ("keyword", "semantic", "hybrid"):
        output[mode] = {key: statistics.mean(row["metrics"][mode][key] for row in rows)
                        for key in rows[0]["metrics"][mode]}
    return {"queries": len(rows), "metrics": output}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        print(json.dumps(self_check(), indent=2))
        return
    root = args.root.resolve()
    data_root = root / "toolret-data"
    output = root / "toolret"
    output.mkdir(parents=True, exist_ok=True)
    download_manifest = json.loads((data_root / "manifest.json").read_bytes())
    for row in download_manifest["files"]:
        if file_sha(data_root / row["path"]) != row["sha256"]:
            raise RuntimeError("Dataset snapshot hash changed")
    records, original_ids, seen_tools = [], {}, set()
    for category in ("code", "customized", "web"):
        table = pq.read_table(data_root / "ToolRet-Tools" / category / "tools-00000-of-00001.parquet")
        for row in table.to_pylist():
            record = CatalogTool.from_definition(category, category, {
                "name": row["id"], "description": row["documentation"], "inputSchema": {},
            })
            if row["id"] in seen_tools:
                raise RuntimeError("Duplicate tool id")
            seen_tools.add(row["id"])
            records.append(record)
            original_ids[record.public_name] = row["id"]
    queries = []
    ids = set(original_ids.values())
    for path in sorted((data_root / "ToolRet-Queries").glob("*/*.parquet")):
        for row in pq.read_table(path).to_pylist():
            labels = json.loads(row["labels"])
            row["task"] = path.parent.name
            row["gold"] = {label["id"]: int(label["relevance"]) for label in labels}
            if not set(row["gold"]).issubset(ids):
                raise RuntimeError(f"Gold tools missing from corpus: {row['id']}")
            queries.append(row)
    assert len({row["id"] for row in queries}) == len(queries)
    checks = self_check(records)
    import torch

    torch.set_num_threads(4)
    embedder = LocalE5Embedder(ToolSearchConfig().model_path)
    fingerprint = embedder.fingerprint()
    manifest = {
        "tools": len(records), "queries": len(queries),
        "query_input": "Original query only; instruction and gold labels excluded",
        "lexical_input": "Full original query, not model-generated keyword reformulation",
        "adapter": "Full raw documentation as description; category and unique tool id kept in search_text",
        "corpus_scope": "All code/customized/web tools for every query; no gold-conditioned sampling",
        "embedding_fingerprint": fingerprint, "embedding_model": "intfloat/multilingual-e5-small",
        "precision": "float32 batched normalized cosine; BM25 float64 with delta=0",
        "fusion": "Production RRF(k=60) on positive top-10 from each branch",
        "dataset_manifest_sha256": file_sha(data_root / "manifest.json"),
        "source_hashes": {str(path.relative_to(PROJECT)): file_sha(path) for path in [
            Path(__file__), PROJECT / "src/kama_claude/core/discovery/embedding.py",
            PROJECT / "src/kama_claude/core/discovery/retrieval.py",
            PROJECT / "src/kama_claude/core/discovery/model.py",
        ]},
    }
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_bytes()) != manifest:
        raise RuntimeError("Frozen ToolRet evaluation changed")
    save(manifest_path, manifest)
    print("ToolRet", len(records), "tools", len(queries), "queries", flush=True)
    vector_path = output / "documents.npy"
    progress_path = output / "embedding-progress.json"
    done = json.loads(progress_path.read_bytes())["documents"] if progress_path.exists() else 0
    if not vector_path.exists():
        dimensions = len(embedder.query("", "read a file"))
        vectors = np.lib.format.open_memmap(vector_path, mode="w+", dtype="float32",
                                            shape=(len(records), dimensions))
    else:
        vectors = np.lib.format.open_memmap(vector_path, mode="r+")
    started = time.perf_counter()
    for begin in range(done, len(records), 256):
        end = min(begin+256, len(records))
        vectors[begin:end] = np.asarray(embedder.documents([r.search_text for r in records[begin:end]]),
                                       dtype=np.float32)
        vectors.flush()
        save(progress_path, {"documents": end, "total": len(records)})
        if begin % 2048 == 0:
            print("embedded documents", end, "/", len(records), flush=True)
    query_path = output / "queries.npy"
    if query_path.exists():
        qvectors = np.load(query_path)
    else:
        qvectors = embedder._model.encode(query_texts(embedder, queries), batch_size=32,
                                          normalize_embeddings=True, show_progress_bar=False)
        np.save(query_path, qvectors)
    # Compare batched preprocessing/encoding against production single-query implementation.
    for i in (0, min(20, len(queries)-1)):
        single = np.asarray(embedder.query("", queries[i]["query"]))
        assert np.allclose(single, qvectors[i], atol=2e-6), "Query embedding mismatch"
    checks["batched_query_vs_production"] = {"queries": 2, "atol": 2e-6, "passed": True}
    save(output / "validation.json", checks)
    index = FrozenBM25(records)
    names = [record.public_name for record in records]
    result_path = output / "results.jsonl"
    existing = ([json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()]
                if result_path.exists() else [])
    done_ids = {row["id"] for row in existing}
    rows = list(existing)
    with result_path.open("a", encoding="utf-8") as target:
        for begin in range(0, len(queries), 64):
            scores = qvectors[begin:begin+64] @ vectors.T
            for offset, query in enumerate(queries[begin:begin+64]):
                if query["id"] in done_ids:
                    continue
                lexical = index.search(query["query"])
                semantic = top(scores[offset], names)
                fused = reciprocal_rank_fusion([lexical, semantic])
                rankings = {mode: [original_ids[name] for name, _ in ranking] for mode, ranking in (
                    ("keyword", lexical), ("semantic", semantic), ("hybrid", fused))}
                row = {"id": query["id"], "task": query["task"], "category": query["category"],
                       "ranking": rankings,
                       "metrics": {mode: metrics(ranking, query["gold"]) for mode, ranking in rankings.items()}}
                rows.append(row)
                target.write(json.dumps(row, ensure_ascii=False)+"\n")
            target.flush()
            save(output / "progress.json", {"completed": len(rows), "planned": len(queries)})
            print("retrieved", len(rows), "/", len(queries), flush=True)
    report = {"all": mean_metrics(rows), "by_category": {}, "by_task": {},
              "elapsed_s_this_process": time.perf_counter()-started}
    for field, key in (("category", "by_category"), ("task", "by_task")):
        for value in sorted({row[field] for row in rows}):
            report[key][value] = mean_metrics([row for row in rows if row[field] == value])
    assert len(rows) == len(queries)
    save(output / "summary.json", report)
    save(output / "progress.json", {"completed": len(rows), "planned": len(queries), "status": "complete"})


if __name__ == "__main__":
    main()
