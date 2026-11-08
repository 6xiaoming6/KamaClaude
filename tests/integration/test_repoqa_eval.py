from __future__ import annotations

import json
from pathlib import Path

from scripts.eval_repoqa import TOOLS, grade, prepare_cases, run_case, summarize

from kama_claude.core.bus.events import LlmUsageEvent
from kama_claude.core.config import KamaConfig
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock

DATA = {
    "python": [{
        "repo": "example/repo",
        "commit_sha": "fixture",
        "content": {"src/code.py": "# fixture\n\ndef double(n):\n    return n * 2\n"},
        "needles": [{
            "description": "Return twice the input number.",
            "path": "src/code.py", "name": "double", "start_line": 2, "end_line": 4,
        }],
    }],
}
ANSWER = {"path": "src/code.py", "symbol": "double", "line": 3}


# 功能：验证判分要求文件、符号和行范围同时匹配。
# 设计：使用只有一个目标的小仓库，单独覆盖缺答和错行。
def test_repoqa_grading(tmp_path: Path) -> None:
    [case] = prepare_cases(DATA, tmp_path, ["python"], seed=42, repos_per_language=1, per_repo=1)
    assert list(case.workspace.rglob("*.py")) == [case.workspace / case.path]
    assert grade(case, json.dumps(ANSWER)) == "correct"
    assert grade(case, json.dumps({**ANSWER, "line": 1})) == "wrong_location"
    assert grade(case, json.dumps({**ANSWER, "line": True})) == "invalid_output"
    assert grade(case, "null") == "no_answer"


# 功能：验证评测通过真实 Runner 读取源码、提交答案并汇总用量。
# 设计：用固定模型响应替身检查白名单和题目边界，避免网络与费用。
async def test_repoqa_runner(tmp_path: Path) -> None:
    [case] = prepare_cases(DATA, tmp_path, ["python"], seed=42, repos_per_language=1, per_repo=1)
    config = KamaConfig()
    config.tool_search.enabled = False

    class Provider:
        calls = 0

        # 首次读取快照，第二次通过 finish 返回结构化定位。
        async def chat(self, messages, tool_schemas, bus, run_id, **kwargs):
            assert set(schema["name"] for schema in tool_schemas) == set(TOOLS["baseline"])
            await bus.publish(LlmUsageEvent(
                run_id=run_id, input_tokens=8, total_input_tokens=10, output_tokens=4,
                cache_read_input_tokens=2, cache_creation_input_tokens=0, ts="test",
            ))
            self.calls += 1
            if self.calls == 1:
                assert "Return twice" in str(messages)
                assert "src/code.py" not in str(messages)
                call = ToolCallBlock("read", "read_file", {"path": "src/code.py"})
            else:
                assert "return n * 2" in str(messages)
                call = ToolCallBlock("finish", "finish", {"result": json.dumps(ANSWER)})
            return LlmResponse("tool_use", tool_calls=[call])

    row = await run_case(case, "baseline", config, tmp_path, 15, provider=Provider())
    assert row["verdict"] == "correct"
    assert row["tool_calls"] == 1
    assert row["input_tokens"] == 20
    assert row["output_tokens"] == 8
    assert (tmp_path / row["events"]).is_file()
    summary = summarize([row], ["baseline"], [case])
    assert summary["baseline"]["all"]["accuracy_at_1"] == 1
