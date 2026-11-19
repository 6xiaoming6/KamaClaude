"""Freeze 200 tools / 30 local servers / 600 queries without metric-based selection."""
from __future__ import annotations

import hashlib
import json
import keyword
import re
from pathlib import Path

import pyarrow.parquet as pq

PROJECT = Path(__file__).resolve().parents[1]
OLD = PROJECT / "runs/mcp-tool-routing-20261116"
ROOT = PROJECT / "runs/mcp-tool-routing-200-20261116"

# Text-only review, before running the expanded evaluation. No retrieval scores were used.
EXCLUSIONS = {
    1: "Equivalent future-value capability to existing compound-interest tool",
    18: "Query asks QR of arbitrary contact text, metadata specifies URL-only input",
    19: "Equivalent compound-interest capability already present",
    28: "Query Power BI resources conflicts with Azure-specific tool description",
    40: "All-market enumeration not clearly supported by asset-ID-based metadata",
    45: "Query Valorant esports conflicts with NBA tool description",
    50: "Adult image search is outside intended developer/office benchmark scope",
    56: "Clothing query conflicts with Wayfair tool description",
    59: "Direct geocoding equivalent already present",
    76: "Creator details query conflicts with product-price details metadata",
    85: "Commodity stories query not clearly aligned with sticker-specific metadata",
    87: "American football query conflicts with ice-hockey metadata",
    99: "Movie showtime/location intent not clearly covered by generic Google search",
    100: "Sentiment capability directly duplicates existing tool",
    105: "Near-duplicate email verification capability in selected pool",
    107: "Trapezoidal integration already represented by another selected tool",
}
TYPE_MAP = {"str": "string", "string": "string", "int": "integer", "integer": "integer",
            "float": "number", "number": "number", "bool": "boolean", "boolean": "boolean"}


def main() -> None:
    candidates_path = ROOT / "selection-candidates.json"
    candidates = json.loads(candidates_path.read_bytes())
    selected = [(i, row) for i, row in enumerate(candidates) if i not in EXCLUSIONS]
    assert len(selected) == 99
    canonical = {}
    corpus = OLD / "toolret-data/ToolRet-Tools/web/tools-00000-of-00001.parquet"
    for row in pq.read_table(corpus).to_pylist():
        canonical[row["id"]] = json.loads(row["documentation"])
    definitions, additions, provenance = [], [], []
    for position, (index, row) in enumerate(selected):
        label, query = row["label"], row["query"]
        doc = canonical[label["id"]]
        assert doc == label["doc"], "Query gold documentation differs from official corpus"
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,43}", doc["name"])
        assert not keyword.iskeyword(doc["name"])
        parameters = {}
        for name, value in doc["parameters"].items():
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,50}", name) and not keyword.iskeyword(name)
            raw_type = value["type"]
            base_type = raw_type.replace(" optional", "").split(",")[0].strip()
            parameters[name] = {"type": TYPE_MAP[base_type],
                                "description": value.get("description", ""),
                                "required": "optional" not in raw_type.lower()}
        server = f"toolret_apigen_{position % 12 + 1:02}"
        definitions.append({"server": server, "name": doc["name"],
                            "description": doc["description"], "parameters": parameters})
        # 95 added query targets, 4 additional tools serve as distractors only.
        is_target = position < 95
        if is_target:
            additions.append({"id": "tr_" + query["id"], "query": query["query"],
                              "expected_server": server, "expected_tool": doc["name"],
                              "source": "ToolRet/APIGen", "original_query_id": query["id"]})
        provenance.append({"candidate_index": index, "source_tool_id": label["id"],
                           "source_query_id": query["id"], "server": server,
                           "name": doc["name"], "has_target_query": is_target,
                           "original_documentation": doc})
    originals = [json.loads(line) for line in (OLD / "upstream-routing/benchmark/dataset.jsonl").read_text(
        encoding="utf-8").splitlines()]
    for row in originals:
        row["source"] = "MCP-routing/original505"
    queries = originals + additions
    assert len(queries) == len({q["id"] for q in queries}) == 600
    assert len({d["server"] for d in definitions}) == 12
    assert len({(d['server'], d['name']) for d in definitions}) == 99
    files = {"extra-tools.json": definitions, "queries.json": queries,
             "selection-provenance.json": {
                 "selection_seed": 2026111602, "eligible_family": "apigen",
                 "candidate_policy": "Seeded unique tools with a single positive label, identifier names, primitive parameters",
                 "selection_candidates_sha256": hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
                 "text_review_exclusions": EXCLUSIONS, "tools": provenance,
                 "grouping": "12 local mock shards, not claims of 12 upstream real API providers",
                 "source_manifest_sha256": hashlib.sha256((OLD/'toolret-data/manifest.json').read_bytes()).hexdigest(),
                 "coverage": "505 existing queries + 95 new target queries; 99 added tools include 4 distractor-only tools",
             }}
    for name, value in files.items():
        text = json.dumps(value, ensure_ascii=False, indent=2)
        path = ROOT / name
        if path.exists() and json.loads(path.read_bytes()) != value:
            raise RuntimeError(f"Refusing to replace frozen data: {name}")
        path.write_text(text, encoding="utf-8")
    print("Frozen: 200 tools, 30 local servers, 600 queries (505 original + 95 ToolRet)")


if __name__ == "__main__":
    main()
