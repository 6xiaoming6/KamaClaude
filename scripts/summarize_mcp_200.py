"""Manually audit and summarize the 30-server, 200-tool local experiment."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import benchmark_mcp_200 as benchmark


def read(path: Path):
    return json.loads(path.read_bytes()) if path.exists() else None


def percent(value):
    return f"{100*value:.2f}%" if value is not None else "—"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args()
    root = benchmark.ROOT / "pilot" if args.pilot else benchmark.ROOT
    summary = read(root / "live-summary.json")
    manifest = read(root / "manifest.json")
    if not summary or not manifest:
        print("No live results yet")
        return
    complete = summary["completed_runs"] == summary["planned_runs"]
    catalog = read(root / "catalog.json")
    assert len(catalog) == 200 and len({r['server'] for r in catalog}) == 30
    cases = {r["id"]: r for r in read(benchmark.ROOT / "queries.json")}
    checked, api_errors = 0, []
    for case_id in manifest["query_ids"]:
        case = cases[case_id]
        gold = case["expected_server"] + "__" + case["expected_tool"]
        for mode in ("all", "deferred"):
            directory = root / "live" / case_id / mode
            row = read(directory / "result.json")
            if not row:
                continue
            assert row["correct"] == (row.get("selected_tool") == gold)
            assert row["expected_tool"] == gold
            if row.get("emitted_business_calls"):
                assert row["selected_tool"] == row["emitted_business_calls"][0]["name"]
                assert row["unexecuted_additional_calls"] == len(row["emitted_business_calls"])-1
            for step, usage in enumerate(row["requests"], 1):
                request = read(directory / f"request-{step:02}.json")
                response = read(directory / f"response-{step:02}.json")
                assert request["messages"][0] == {"role": "user", "content": case["query"]}
                tools = [tool for tool in request["tools"] if "__" in tool["name"]]
                assert len(tools) == 200
                assert all(bool(t.get("defer_loading")) == (mode == "deferred") for t in tools)
                raw = response["usage"]
                total = raw["input_tokens"] + (raw.get("cache_read_input_tokens") or 0) + (
                    raw.get("cache_creation_input_tokens") or 0)
                assert usage["total_input_tokens"] == total
                assert usage["output_tokens"] == raw["output_tokens"]
            assert row["total_input_tokens"] == sum(r["total_input_tokens"] for r in row["requests"])
            assert row["output_tokens"] == sum(r["output_tokens"] for r in row["requests"])
            if not row["usage_complete"]:
                api_errors.append({"id": case_id, "mode": mode, "error": row.get("error")})
            checked += 1
    assert checked == summary["completed_runs"]
    benchmark.base.save(root / "artifact-validation.json", {
        "completed": complete, "checked_runs": checked, "tools": 200, "servers": 30,
        "raw_response_usage_checked": True, "query_and_gold_checked": True, "api_errors": api_errors,
        "scope": "Artifact consistency; no claim of parameter semantic or real business correctness",
    })
    lines = ["# 30 个本地 MCP／200 工具／600 查询配对验证", "",
             f"更新：{datetime.now().astimezone().isoformat(timespec='seconds')}", "",
             "状态：" + ("执行结束，异常保留" if complete else "执行中"), "",
             f"已处理 {summary['completed_runs']} / {summary['planned_runs']} 次运行。", "",
             "## 两组结果", "",
             "| 方案 | 已测 | 选对工具 | 正确率 | 输入Token | 输出Token | 中位耗时 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for mode, row in summary["modes"].items():
        lines.append(f"| {mode} | {row['completed']} | {row['correct']} | {percent(row['accuracy'])} | "
                     f"{row['total_input_tokens']:,} | {row['output_tokens']:,} | {row['median_elapsed_s']:.2f}s |")
    lines += ["", "## 按查询来源分组", "",
              "| 来源 | 每组计划题数 | 全量组正确率 | 渐进组正确率 |", "|---|---:|---:|---:|"]
    for source, row in summary["by_query_source"].items():
        lines.append(f"| {source} | {row['planned_queries']} | "
                     f"{percent(row['modes']['all']['accuracy'])} | {percent(row['modes']['deferred']['accuracy'])} |")
    lines += ["", "## 成对用量", ""]
    for key in ("paired_all_usage_complete", "paired_both_correct"):
        row = summary[key]
        lines += [f"- {key}：{row['pairs']} 对；输入 Token 减少 "
                  f"{percent(row['total_input_tokens']['reduction'])}，输入＋输出 Token 减少 "
                  f"{percent(row['total_tokens']['reduction'])}。"]
    lines += ["", "## 异常与边界", "", "```json",
              json.dumps({m: r["status"] for m, r in summary["modes"].items()}, indent=2), "```", "",
              "- 200 为业务工具数；渐进组另外有2个常驻发现工具。两组共享相同的200个业务工具。",
              "- 18个原模拟服务，另12个本地实例承载从ToolRet提取的99个工具；这12个实例是模拟分组，不是12个真实API供应商。",
              "- 505条原查询加95条ToolRet原始查询；新增99个工具中有4个只作为干扰项。",
              "- 新增数据在本轮模型评测前按文本一致性和Schema可转换性选定，保留候选、排除原因与来源。",
              "- 只测首次业务工具选择与模拟调用；占位参数通过Schema不代表语义正确或业务完成。",
              "- 全部模型往返及失败保留，总输入包括缓存读取；费用变化不一定与Token变化同比。",
              "- 用量比较只使用两组Usage都完整的配对，同时报告被排除的异常，不选择最好重试结果。",
              "- 原101工具的结果单独保留；规模对比应首先看相同505条查询子集，不能混淆查询构成变化。", ""]
    (root / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print("complete" if complete else "partial report verified and written")


if __name__ == "__main__":
    main()
