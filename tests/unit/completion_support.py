from __future__ import annotations

from kama_claude.core.llm.types import LlmResponse, ToolCallBlock


def submit_response(response: LlmResponse, tool_schemas, run_id: str, step: int = 0):
    """旧测试桩的最终答复按可见协议提交；摘要请求仍返回普通文本。"""
    if (run_id != "compact" and response.stop_reason == "end_turn"
            and not response.tool_calls
            and any(schema["name"] == "finish" for schema in tool_schemas)):
        return LlmResponse(
            "tool_use", tool_calls=[ToolCallBlock(
                f"finish-{step}", "finish", {"result": response.text or "done"},
            )], usage=response.usage, thinking_blocks=response.thinking_blocks,
        )
    return response
