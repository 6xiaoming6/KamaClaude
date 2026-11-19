"""Paired live tool routing evaluation; real local MCP, production retrieval/loading.

No business services or user files are accessed. Gold labels are only used after
the first business call, never to generate search keywords or choose a tool.
"""
# ruff: noqa: I001 -- provider must initialize before estimator to avoid existing package cycle
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import jsonschema

from kama_claude.core.config import McpServerConfig, ToolSearchConfig, get_config
from kama_claude.core.llm import provider as _provider  # noqa: F401 -- initialize package first
from kama_claude.core.discovery.embedding import LocalE5Embedder
from kama_claude.core.discovery.exposure import callable_names, request_schemas
from kama_claude.core.discovery.model import CatalogTool
from kama_claude.core.discovery.retrieval import ToolRetriever
from kama_claude.core.discovery.store import CatalogStore
from kama_claude.core.discovery.tool import GetToolSchema, SearchToolCandidates
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.checkpoint.checkpoint import RunCheckpoint

PROJECT = Path(__file__).resolve().parents[1]
SYSTEM = """You are completing a tool-routing benchmark using local synthetic MCP services.
Choose exactly one business tool that best matches the user's request and invoke it.
All business tools produce deterministic fabricated results and have no external side effects.
Use values explicitly provided in the request. If a required value is missing, use
the literal __UNSPECIFIED__ for strings, 0 for numbers, false for booleans, [] for arrays,
or {} for objects. Do not ask clarification questions in this routing-only benchmark.
When discovery tools are available, first call search_tool_candidates with a capability
description and 1-3 keywords inferred ONLY from the request. Inspect candidates on the
next turn, then use get_tool_schema to load only the needed tool. The loaded tool can
be invoked on the NEXT turn. If candidates do not fit, refine the query or increase topk.
Never invent a tool name. Do not perform unrelated operations. Keep reasoning concise.
"""


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def make_run(records: list[CatalogTool], query: str, mode: str) -> RunCheckpoint:
    run = RunCheckpoint(
        run_id="routing", goal=query, user_input=query, model="benchmark",
        workspace=str(PROJECT), max_steps=8, tool_loading_mode=mode,
        mcp_tool_names=[record.public_name for record in records],
        tool_schemas=[record.schema for record in records], max_loaded_schema_tokens=10485,
    )
    if mode == "deferred":
        for cls in (SearchToolCandidates, GetToolSchema):
            run.tool_schemas.insert(0, {
                "name": cls.name, "description": cls.description, "input_schema": cls.input_schema,
            })
    return run


async def offline(records: list[CatalogTool], queries: list[dict[str, Any]], root: Path,
                  retriever: ToolRetriever) -> None:
    rows = []
    # The full original query is available to all modes; no label-derived keywords.
    for i, case in enumerate(queries):
        gold = case["expected_server"] + "__" + case["expected_tool"]
        for mode in ("keyword", "semantic", "hybrid"):
            started = time.perf_counter()
            result = await retriever.search(records, "", case["query"], [case["query"]], mode=mode)
            names = [name for name, _ in result.ranking]
            rank = names.index(gold) + 1 if gold in names else None
            rows.append({"id": case["id"], "mode": mode, "rank": rank,
                         "ranking": names, "method": result.method,
                         "degraded_reason": result.reason,
                         "elapsed_ms": (time.perf_counter()-started)*1000})
        if (i+1) % 50 == 0:
            print("offline", i+1, "/", len(queries), flush=True)
    save(root / "offline-results.json", rows)
    report = {}
    for mode in ("keyword", "semantic", "hybrid"):
        group = [row for row in rows if row["mode"] == mode]
        report[mode] = {
            "queries": len(group),
            **{f"hit_at_{k}": sum(row["rank"] is not None and row["rank"] <= k
                                  for row in group)/len(group) for k in (1, 3, 5, 10)},
            "mrr_at_10": statistics.mean(1/row["rank"] if row["rank"] else 0 for row in group),
            "degraded": sum(bool(row["degraded_reason"]) for row in group),
        }
    save(root / "offline-summary.json", report)


async def route(case: dict[str, Any], mode: str, records: list[CatalogTool],
                manager: McpServerManager, retriever: ToolRetriever, client: Any,
                model: str, directory: Path) -> dict[str, Any]:
    run = make_run(records, case["query"], mode)
    search = SearchToolCandidates(run, records, retriever)
    loader = GetToolSchema(run)
    business = {tool.name: tool for tool in manager.get_tools()}
    messages: list[Any] = [{"role": "user", "content": case["query"]}]
    row: dict[str, Any] = {"id": case["id"], "mode": mode, "correct": False,
                           "mcp_call_succeeded": False, "schema_valid": False,
                           "requests": [], "status": "step_limit", "usage_complete": True}
    started = time.perf_counter()
    for step in range(1, 9):
        run.step = step
        schemas = request_schemas(run)
        allowed = callable_names(run, messages)
        payload: dict[str, Any] = {
            "model": model, "system": SYSTEM, "messages": messages, "tools": schemas,
            "max_tokens": 768, "extra_body": {"temperature": 0}, "thinking": {"type": "disabled"},
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        }
        save(directory / f"request-{step:02}.json", payload)
        before = time.perf_counter()
        try:
            async with asyncio.timeout(180):
                response = await client.messages.create(**payload)
        except Exception as exc:
            row.update(status="api_error", error=f"{type(exc).__name__}: {exc}", usage_complete=False)
            break
        raw = response.model_dump(exclude_none=True)
        save(directory / f"response-{step:02}.json", raw)
        usage = response.usage
        plain = usage.input_tokens
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        created = getattr(usage, "cache_creation_input_tokens", 0) or 0
        row["requests"].append({
            "input_tokens": plain, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": created, "total_input_tokens": plain+cached+created,
            "output_tokens": usage.output_tokens, "elapsed_s": time.perf_counter()-before,
            "stop_reason": response.stop_reason,
            "serialized_request_args_bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
        })
        messages.append({"role": "assistant", "content": raw["content"]})
        calls = [block for block in response.content if block.type == "tool_use"]
        if len(calls) != 1:
            row.update(status="invalid_call_count", call_count=len(calls))
            break
        call = calls[0]
        # Eligibility frozen before the request, not after processing same-step loads.
        if call.name not in allowed:
            output, error = "Tool unavailable on this model turn; search and load it first.", True
        elif call.name == search.name:
            try:
                result = await search.invoke(call.input)
                output, error = result.message_content(), result.is_error
            except Exception as exc:
                output, error = f"Invalid search arguments: {exc}", True
        elif call.name == loader.name:
            try:
                result = await loader.invoke(call.input)
                output, error = result.message_content(), result.is_error
            except Exception as exc:
                output, error = f"Invalid load arguments: {exc}", True
        elif call.name in business:
            row["selected_tool"] = call.name
            row["arguments"] = call.input
            try:
                jsonschema.Draft202012Validator(business[call.name].input_schema).validate(call.input)
                row["schema_valid"] = True
            except jsonschema.ValidationError as exc:
                row["schema_error"] = exc.message
            # The official synthetic execution goes through the real MCP connection.
            result = await business[call.name].invoke(call.input)
            row["mcp_call_succeeded"] = not result.is_error
            row["mcp_result"] = result.content
            row["status"] = "routed"
            break
        else:
            output, error = "Unknown tool", True
        messages.append({"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": error,
        }]})
    row["elapsed_s"] = time.perf_counter()-started
    row["request_count"] = len(row["requests"])
    for key in ("total_input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens", "input_tokens", "serialized_request_args_bytes"):
        row[key] = sum(item[key] for item in row["requests"])
    row["total_tokens"] = row["total_input_tokens"] + row["output_tokens"]
    # Gold is consulted only after the routing trajectory has ended.
    row["expected_tool"] = case["expected_server"] + "__" + case["expected_tool"]
    row["correct"] = row.get("selected_tool") == row["expected_tool"]
    save(directory / "result.json", row)
    return row


def summarize(root: Path, cases: list[dict[str, Any]]) -> None:
    results = []
    for case in cases:
        for mode in ("all", "deferred"):
            path = root / "live" / case["id"] / mode / "result.json"
            if path.exists():
                results.append(json.loads(path.read_bytes()))
    report: dict[str, Any] = {"planned_queries": len(cases), "planned_runs": 2*len(cases),
                              "completed_runs": len(results), "modes": {}}
    for mode in ("all", "deferred"):
        rows = [row for row in results if row["mode"] == mode]
        if not rows:
            continue
        report["modes"][mode] = {
            "completed": len(rows), "correct": sum(row["correct"] for row in rows),
            "accuracy": sum(row["correct"] for row in rows)/len(rows),
            "status": dict(Counter(row["status"] for row in rows)),
            "schema_valid": sum(row["schema_valid"] for row in rows),
            "mcp_call_succeeded": sum(row["mcp_call_succeeded"] for row in rows),
            "usage_complete": all(row["usage_complete"] for row in rows),
            **{key: sum(row[key] for row in rows) for key in (
                "total_input_tokens", "output_tokens", "total_tokens", "request_count",
                "cache_read_input_tokens", "input_tokens", "serialized_request_args_bytes")},
            "median_elapsed_s": statistics.median(row["elapsed_s"] for row in rows),
        }
    by_pair = {(row["id"], row["mode"]): row for row in results}
    pairs = [(by_pair[(c["id"], "all")], by_pair[(c["id"], "deferred")]) for c in cases
             if (c["id"], "all") in by_pair and (c["id"], "deferred") in by_pair]
    complete = [(a, b) for a, b in pairs if a["usage_complete"] and b["usage_complete"]]
    both_correct = [(a, b) for a, b in complete if a["correct"] and b["correct"]]
    for name, subset in (("paired_all_usage_complete", complete), ("paired_both_correct", both_correct)):
        report[name] = {"pairs": len(subset)}
        for key in ("total_input_tokens", "total_tokens"):
            base = sum(a[key] for a, _ in subset)
            progressive = sum(b[key] for _, b in subset)
            report[name][key] = {"all": base, "deferred": progressive,
                                 "reduction": 1-progressive/base if base else None}
    save(root / "live-summary.json", report)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT / "runs/mcp-tool-routing-20261116")
    parser.add_argument("--phase", choices=["offline", "live", "all", "summary"], default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    upstream = root / "upstream-routing"
    cases = load_rows(upstream / "benchmark/dataset.jsonl")
    assert len(cases) == len({c["id"] for c in cases}) == 505
    assert len({(c["expected_server"], c["expected_tool"]) for c in cases}) == 101
    # Frozen random task order; balanced arm order avoids always warming one arm first.
    random.Random(20261116).shuffle(cases)
    if args.limit:
        cases = cases[:args.limit]
    if args.pilot:
        root = root / "pilot"
    root.mkdir(parents=True, exist_ok=True)
    if args.phase == "summary":
        summarize(root, cases)
        return
    config = get_config()
    dataset = json.loads((upstream / "data/tools.json").read_bytes())
    search_config = ToolSearchConfig(db_path=str(root / "catalog.sqlite3"),
                                     model_path=config.tool_search.model_path)
    manager = McpServerManager(search_config)
    configs = [McpServerConfig(
        name=server["server_id"], command=sys.executable,
        args=[str(PROJECT / "scripts/mcp_benchmark_server.py"), str(upstream), server["server_id"]],
    ) for server in dataset["servers"]]
    try:
        await manager.start_all(configs)
        tools = manager.get_tools()
        assert len(tools) == 101, f"Expected 101 tools, connected {len(tools)}"
        records = [tool.record for tool in tools]
        assert all(record is not None for record in records)
        catalog = [{"server": record.server_name, "definition": json.loads(record.definition_json)}
                   for record in records]
        catalog_path = root / "catalog.json"
        if catalog_path.exists() and json.loads(catalog_path.read_bytes()) != catalog:
            raise RuntimeError("MCP catalog changed during resume")
        save(catalog_path, catalog)
        hashes = {path: sha(upstream / path) for path in (
            "data/tools.json", "benchmark/dataset.jsonl", "synthetic_servers/base.py",
            "synthetic_servers/synthetic.py")}
        manifest = {"upstream_commit": subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip(),
            "hashes": hashes, "query_ids": [c["id"] for c in cases],
            "model": config.llm.default_model, "temperature": 0, "thinking": "disabled",
            "max_steps": 8, "max_output_tokens_per_request": 768,
            "request_wall_timeout_s": 180,
            "max_loaded_schema_tokens": 10485,
            "source_hashes": {str(path.relative_to(PROJECT)): sha(path) for path in [
                Path(__file__), PROJECT / "scripts/mcp_benchmark_server.py",
                *sorted((PROJECT / "src/kama_claude/core/discovery").glob("*.py")),
                PROJECT / "src/kama_claude/core/llm/tool_loading.py",
            ]},
            "protocol": "First business tool routing + real synthetic MCP invocation; not task completion",
            "missing_argument_policy": "explicit placeholders; schema validity is not semantic correctness",
            "gold_visibility": "labels never enter requests, search or runtime feedback",
            "system": SYSTEM,
        }
        path = root / "manifest.json"
        if path.exists() and json.loads(path.read_bytes()) != manifest:
            raise RuntimeError("Manifest changed; use a new output directory")
        save(path, manifest)
        import torch

        torch.set_num_threads(4)
        embedder = LocalE5Embedder(search_config.model_path)
        fingerprint = await asyncio.to_thread(embedder.fingerprint)
        save(root / "embedding.json", {"fingerprint": fingerprint})
        retriever = ToolRetriever(CatalogStore(root / "vectors.sqlite3"), search_config, embedder)
        if args.phase in ("offline", "all"):
            await offline(records, cases, root, retriever)
        if args.phase in ("live", "all"):
            key = os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                raise RuntimeError("DEEPSEEK_API_KEY missing")
            async with anthropic.AsyncAnthropic(
                api_key=key, base_url="https://api.deepseek.com/anthropic", max_retries=0,
                timeout=120, http_client=httpx2.AsyncClient(trust_env=False, timeout=120),
            ) as client:
                consecutive_api_errors = 0
                for i, case in enumerate(cases):
                    order = ("all", "deferred") if i % 2 == 0 else ("deferred", "all")
                    for mode in order:
                        directory = root / "live" / case["id"] / mode
                        if (directory / "result.json").exists():
                            continue
                        save(root / "progress.json", {"current": case["id"], "mode": mode,
                                                       "position": i+1, "planned": len(cases)})
                        row = await route(case, mode, records, manager, retriever, client,
                                          config.llm.default_model, directory)
                        print(i+1, "/", len(cases), case["id"], mode, row["status"],
                              row["correct"], row["total_input_tokens"], flush=True)
                        summarize(root, cases)
                        consecutive_api_errors = consecutive_api_errors+1 if row["status"] == "api_error" else 0
                        if consecutive_api_errors >= 3:
                            raise RuntimeError("Three consecutive API errors; batch stopped")
            save(root / "progress.json", {"current": None, "status": "complete"})
        summarize(root, cases)
    finally:
        await manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
