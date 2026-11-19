"""Manually audit benchmark artifacts and render a current report."""
from __future__ import annotations

import json
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "runs/mcp-tool-routing-20261116"


def read(path: Path):
    return json.loads(path.read_bytes()) if path.exists() else None


def pct(value: float) -> str:
    return f"{value*100:.2f}%"


def write_report(root: Path) -> bool:
    live = read(root / "live-summary.json")
    offline = read(root / "offline-summary.json")
    toolret = read(root / "toolret/summary.json")
    complete = bool(live and live["completed_runs"] == 1010 and toolret)
    lines = ["# KamaClaude MCP 评测结果", "",
             f"报告生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}", "",
             "状态：" + ("全部执行结束（错误仍按原样保留）" if complete else "执行中／尚未全部完成"), "",
             "## 固定查询检索消融（505 条）", "",
             "输入为原始查询，不使用模型改写或正确答案。", ""]
    if offline:
        lines += ["| 方案 | Hit@1 | Hit@3 | Hit@5 | MRR@10 |", "|---|---:|---:|---:|---:|"]
        for mode, row in offline.items():
            lines.append(f"| {mode} | {pct(row['hit_at_1'])} | {pct(row['hit_at_3'])} | "
                         f"{pct(row['hit_at_5'])} | {row['mrr_at_10']:.4f} |")
    lines += ["", "## 本地 MCP 配对实测", ""]
    if live:
        lines += [f"已完成 {live['completed_runs']} / {live['planned_runs']} 次运行。", "",
                  "| 方案 | 已测 | 选对工具 | 正确率 | 总输入 Token | 总输出 Token | 有Usage请求数 | 中位耗时 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for mode, row in live["modes"].items():
            lines.append(f"| {mode} | {row['completed']} | {row['correct']} | {pct(row['accuracy'])} | "
                         f"{row['total_input_tokens']:,} | {row['output_tokens']:,} | "
                         f"{row['request_count']} | {row['median_elapsed_s']:.2f}s |")
        for label, key in (("两组 Usage 均完整", "paired_all_usage_complete"),
                           ("两组均选对工具且 Usage 完整", "paired_both_correct")):
            row = live[key]
            lines += ["", f"{label}：{row['pairs']} 对。"]
            for metric, title in (("total_input_tokens", "总输入 Token"), ("total_tokens", "输入＋输出 Token")):
                values = row[metric]
                if values["reduction"] is not None:
                    lines.append(f"- {title}：{values['all']:,} → {values['deferred']:,}，减少 {pct(values['reduction'])}。")
        lines += ["", "错误与调用情况：", "", "```json",
                  json.dumps({mode: {key: row[key] for key in (
                      "status", "schema_valid", "mcp_call_succeeded", "usage_complete",
                      "cache_read_input_tokens", "input_tokens")}
                              for mode, row in live["modes"].items()}, ensure_ascii=False, indent=2), "```"]
    lines += ["", "## ToolRet 全量离线检索", ""]
    if toolret:
        lines += [f"查询数：{toolret['all']['queries']}；完整工具库：44,453。", "",
                  "| 方案 | Recall@3 | Recall@5 | Recall@10 | Complete@10 | NDCG@10 | MRR@10 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for mode, row in toolret["all"]["metrics"].items():
            lines.append(f"| {mode} | {pct(row['recall_at_3'])} | {pct(row['recall_at_5'])} | "
                         f"{pct(row['recall_at_10'])} | {pct(row['complete_at_10'])} | "
                         f"{row['ndcg_at_10']:.4f} | {row['mrr_at_10']:.4f} |")
    else:
        progress = read(root / "toolret/embedding-progress.json") or {}
        lines += ["尚未全部完成。向量编码进度：" + json.dumps(progress, ensure_ascii=False)]
    lines += ["", "## 解释边界", "",
              "- 505 查询的测试终点是首次业务工具路由，不代表完整业务任务完成或参数语义正确。",
              "- 本地服务提供确定性模拟结果；没有连接外部业务系统。",
              "- 实测复用生产工具检索、加载、调用资格及 MCP 通信；模型请求由独立评测驱动器发起，非完整默认 AgentLoop。",
              "- 渐进组统计所有搜索、加载等模型往返；总输入包括缓存读取，Token 减少不等于费用按相同比例减少。",
              "- 原生延迟模式仍向服务端发送完整工具目录，未声称节省网络带宽。",
              "- ToolRet 使用原始查询与全量工具库的固定适配协议，不是官方排行榜提交。",
              "- 没有从多个模型尝试中挑选最好答案，试运行不混入正式成绩。",
              "- 数据集标签和工具描述可能存在歧义；本轮保留原始标签，不因结果不利而更换。",
              "- 第125题渐进组第三个请求长时间未返回，保留为API异常且Usage不完整；后续请求增加180秒墙钟截止，原版本与修订记录保留。",
              "- 协议及版本详见 docs/MCP-Benchmark-Protocol.md；逐题证据保存在本目录。", ""]
    (root / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    if complete:
        verify_live(root, live)
    return complete


def verify_live(root: Path, summary: dict) -> None:
    cases = [json.loads(line) for line in (root / "upstream-routing/benchmark/dataset.jsonl").read_text(
        encoding="utf-8").splitlines()]
    checked, api_errors = 0, []
    for case in cases:
        expected = case["expected_server"] + "__" + case["expected_tool"]
        for mode in ("all", "deferred"):
            directory = root / "live" / case["id"] / mode
            row = read(directory / "result.json")
            assert row and row["id"] == case["id"] and row["mode"] == mode
            assert row["correct"] == (row.get("selected_tool") == expected)
            assert row["expected_tool"] == expected
            for step, usage in enumerate(row["requests"], 1):
                request = read(directory / f"request-{step:02}.json")
                response = read(directory / f"response-{step:02}.json")
                assert request["messages"][0] == {"role": "user", "content": case["query"]}
                actual = response["usage"]
                actual_total = actual["input_tokens"] + (actual.get("cache_read_input_tokens") or 0) + (
                    actual.get("cache_creation_input_tokens") or 0)
                assert usage["total_input_tokens"] == actual_total
                assert usage["output_tokens"] == actual["output_tokens"]
                tools = [item for item in request["tools"] if "__" in item["name"]]
                assert len(tools) == 101
                assert all(bool(item.get("defer_loading")) == (mode == "deferred") for item in tools)
            assert row["total_input_tokens"] == sum(item["total_input_tokens"] for item in row["requests"])
            assert row["output_tokens"] == sum(item["output_tokens"] for item in row["requests"])
            if not row["usage_complete"]:
                api_errors.append({"id": row["id"], "mode": mode, "error": row.get("error")})
            checked += 1
    assert checked == 1010 == summary["completed_runs"]
    validation = {"live_runs_checked": checked, "request_usage_recomputed": True,
                  "gold_mapping_checked": True, "all_101_business_schemas_checked": True,
                  "api_errors": api_errors,
                  "verification_scope": "artifact consistency, not independent business correctness",
                  "packages": {name: version(name) for name in (
                      "anthropic", "mcp", "sentence-transformers", "torch", "numpy", "pyarrow")}}
    (root / "artifact-validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")


def main() -> None:
    complete = write_report(ROOT)
    print("complete" if complete else "partial report written", flush=True)


if __name__ == "__main__":
    main()
