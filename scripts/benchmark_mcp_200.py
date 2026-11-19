"""200 business tools / 30 local MCP services / 600 paired routing queries."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path

import anthropic
import benchmark_mcp_routing as base
import httpx2
from mcp_routing_200_route import route

from kama_claude.core.config import McpServerConfig, ToolSearchConfig, get_config
from kama_claude.core.discovery.embedding import LocalE5Embedder
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.discovery.store import CatalogStore
from kama_claude.core.mcp.server import McpServerManager

PROJECT = Path(__file__).resolve().parents[1]
OLD = PROJECT / "runs/mcp-tool-routing-20261116"
ROOT = PROJECT / "runs/mcp-tool-routing-200-20261116"


def summarize(root: Path, cases: list[dict]) -> None:
    base.summarize(root, cases)
    report = json.loads((root / "live-summary.json").read_bytes())
    report.update(business_tools=200, local_mcp_servers=30, by_query_source={})
    for source in sorted({c["source"] for c in cases}):
        subset = [c for c in cases if c["source"] == source]
        source_report = {"planned_queries": len(subset), "modes": {}}
        for mode in ("all", "deferred"):
            paths = [root / "live" / c["id"] / mode / "result.json" for c in subset]
            rows = [json.loads(p.read_bytes()) for p in paths if p.exists()]
            source_report["modes"][mode] = {
                "completed": len(rows), "correct": sum(row["correct"] for row in rows),
                "accuracy": sum(row["correct"] for row in rows)/len(rows) if rows else None,
            }
        report["by_query_source"][source] = source_report
    base.save(root / "live-summary.json", report)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--phase", choices=("all", "live", "summary"), default="all")
    args = parser.parse_args()
    cases = json.loads((ROOT / "queries.json").read_bytes())
    assert len(cases) == len({c["id"] for c in cases}) == 600
    original = [c for c in cases if c["source"] == "MCP-routing/original505"]
    additional = [c for c in cases if c["source"] == "ToolRet/APIGen"]
    assert len(original) == 505 and len(additional) == 95
    random.Random(2026111602).shuffle(cases)
    root = ROOT / "pilot" if args.pilot else ROOT
    if args.pilot:
        # Exercise both source subsets before the formal batch; no formal results reused.
        cases = [original[0], original[-1], additional[0], additional[-1]]
    root.mkdir(parents=True, exist_ok=True)
    if args.phase == "summary":
        summarize(root, cases)
        return
    config = get_config()
    old_manifest = json.loads((OLD / "manifest.json").read_bytes())
    if config.llm.default_model != old_manifest["model"]:
        raise RuntimeError("Model changed from 101-tool run; choose comparison baseline explicitly")
    upstream = OLD / "upstream-routing"
    dataset = json.loads((upstream / "data/tools.json").read_bytes())
    additions = json.loads((ROOT / "extra-tools.json").read_bytes())
    search_config = ToolSearchConfig(db_path=str(root / "catalog.sqlite3"),
                                     model_path=config.tool_search.model_path)
    manager = McpServerManager(search_config)
    configs = [McpServerConfig(
        name=s["server_id"], command=sys.executable,
        args=[str(PROJECT / "scripts/mcp_benchmark_server.py"), str(upstream), s["server_id"]],
    ) for s in dataset["servers"]]
    configs.extend(McpServerConfig(
        name=name, command=sys.executable,
        args=[str(PROJECT / "scripts/mcp_toolret_mock_server.py"), str(ROOT / "extra-tools.json"), name],
    ) for name in sorted({d["server"] for d in additions}))
    assert len(configs) == len({c.name for c in configs}) == 30
    try:
        await manager.start_all(configs)
        tools = manager.get_tools()
        assert len(tools) == len({tool.name for tool in tools}) == 200, len(tools)
        assert len({tool.server_name for tool in tools}) == 30
        expected = {c["expected_server"] + "__" + c["expected_tool"] for c in cases}
        assert expected.issubset({tool.name for tool in tools})
        records = [tool.record for tool in tools]
        assert all(record is not None for record in records)
        catalog = [{"server": r.server_name, "definition": json.loads(r.definition_json)} for r in records]
        path = root / "catalog.json"
        if path.exists() and json.loads(path.read_bytes()) != catalog:
            raise RuntimeError("Frozen 200-tool catalog changed")
        base.save(path, catalog)
        paths = [Path(__file__), PROJECT / "scripts/benchmark_mcp_routing.py",
                 PROJECT / "scripts/mcp_benchmark_server.py", PROJECT / "scripts/mcp_toolret_mock_server.py",
                 PROJECT / "scripts/prepare_mcp_200.py",
                 PROJECT / "scripts/mcp_routing_200_route.py",
                 *sorted((PROJECT / "src/kama_claude/core/discovery").glob("*.py")),
                 PROJECT / "src/kama_claude/core/llm/tool_loading.py"]
        manifest = {"business_tools": 200, "local_mcp_servers": 30, "queries": len(cases),
                    "query_ids": [c["id"] for c in cases], "model": config.llm.default_model,
                    "system": base.SYSTEM, "max_steps": 8, "max_output_tokens_per_request": 768,
                    "request_wall_timeout_s": 180, "temperature": 0, "thinking": "disabled",
                    "max_loaded_schema_tokens": 10485, "seed": 2026111602,
                    "schema_limit_scope": "200 business tools; deferred group additionally exposes 2 discovery tools",
                    "source_hashes": {str(p.relative_to(PROJECT)): base.sha(p) for p in paths},
                    "dataset_hashes": {name: base.sha(ROOT / name) for name in (
                        "extra-tools.json", "queries.json", "selection-provenance.json", "selection-candidates.json")},
                    "upstream_commit": old_manifest["upstream_commit"],
                    "upstream_hashes": old_manifest["hashes"],
                    "protocol": "First business tool routing and real local synthetic MCP invocation; no real API execution",
                    "gold_visibility": "Labels used only after routing; never used to generate model search keywords",
                    "service_grouping": "18 original services + 12 local shards carrying 99 ToolRet tools",
                    "query_coverage": "505 original + 95 ToolRet single-positive queries; 4 new tools are distractor-only"}
        manifest["multiple_business_calls"] = (
            "Only the first business call is executed and scored; additional calls recorded as unexecuted. "
            "Discovery/business mixtures on the same turn remain invalid."
        )
        path = root / "manifest.json"
        if path.exists() and json.loads(path.read_bytes()) != manifest:
            raise RuntimeError("Frozen benchmark changed; use a new output directory")
        base.save(path, manifest)
        import torch

        torch.set_num_threads(4)
        embedder = LocalE5Embedder(search_config.model_path)
        fingerprint = await asyncio.to_thread(embedder.fingerprint)
        if fingerprint != json.loads((OLD / "embedding.json").read_bytes())["fingerprint"]:
            raise RuntimeError("Embedding model changed from previous comparison")
        base.save(root / "embedding.json", {"fingerprint": fingerprint})
        retriever = ToolRetriever(CatalogStore(root / "vectors.sqlite3"), search_config, embedder)
        if args.phase == "all":
            await base.offline(records, cases, root, retriever)
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY missing")
        async with anthropic.AsyncAnthropic(
            api_key=key, base_url="https://api.deepseek.com/anthropic", max_retries=0,
            timeout=120, http_client=httpx2.AsyncClient(trust_env=False, timeout=120),
        ) as client:
            errors = 0
            for i, case in enumerate(cases):
                order = ("all", "deferred") if i % 2 == 0 else ("deferred", "all")
                for mode in order:
                    directory = root / "live" / case["id"] / mode
                    if (directory / "result.json").exists():
                        continue
                    base.save(root / "progress.json", {"current": case["id"], "source": case["source"],
                                                       "mode": mode, "position": i+1,
                                                       "planned": len(cases), "status": "running"})
                    row = await route(case, mode, records, manager, retriever, client,
                                      config.llm.default_model, directory)
                    print(i+1, "/", len(cases), case["id"], mode, row["status"],
                          row["correct"], row["total_input_tokens"], flush=True)
                    summarize(root, cases)
                    errors = errors+1 if row["status"] == "api_error" else 0
                    if errors >= 3:
                        raise RuntimeError("Three consecutive API errors; stopped without overwriting results")
        base.save(root / "progress.json", {"current": None, "status": "complete"})
        summarize(root, cases)
    finally:
        await manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
