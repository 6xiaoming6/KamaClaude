"""Explicit live probe: at most two billed DeepSeek requests; no project data or business execution."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import anthropic
from dotenv import load_dotenv

from kama_claude.core.discovery.tool import GetToolSchemaParams


async def check() -> None:
    load_dotenv()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not configured; live probe skipped")
    tools: list[Any] = [
        {"name": "get_tool_schema", "description": (
            "Load verification_echo with names=['verification_echo']; after receiving its reference, "
            "call the referenced tool with text hello."),
         "input_schema": GetToolSchemaParams.model_json_schema(), "defer_loading": False},
        {"name": "verification_echo", "description": "Echo the given text without side effects.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"]}, "defer_loading": True},
    ]
    messages: list[Any] = [{"role": "user", "content": "hello"}]
    async with anthropic.AsyncAnthropic(
        api_key=key, base_url="https://api.deepseek.com/anthropic", max_retries=0, timeout=60,
    ) as client:
        options: dict[str, Any] = {
            "model": os.environ.get("KAMA_TEST_DEEPSEEK_MODEL", "deepseek-flash"),
            "max_tokens": 256, "thinking": {"type": "disabled"}, "tools": tools,
        }
        first = await client.messages.create(
            **options, messages=messages, tool_choice={"type": "tool", "name": "get_tool_schema"},
        )
        calls = [block for block in first.content if block.type == "tool_use"]
        if len(calls) != 1 or calls[0].name != "get_tool_schema":
            raise RuntimeError("first response did not produce the requested schema loading call")
        if GetToolSchemaParams.model_validate(calls[0].input).names != ["verification_echo"]:
            raise RuntimeError("schema loading did not select the verification tool")
        messages.extend([
            {"role": "assistant", "content": [block.model_dump(exclude_none=True) for block in first.content]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0].id,
                "content": [{"type": "tool_reference", "tool_name": "verification_echo"}]}]},
        ])
        second = await client.messages.create(**options, messages=messages)
        calls = [block for block in second.content if block.type == "tool_use"]
        if not any(block.name == "verification_echo" and block.input == {"text": "hello"} for block in calls):
            raise RuntimeError("referenced tool was not called with the expected arguments")
        print(json.dumps({
            "model": options["model"], "defer_loading": True, "tool_reference": True,
            "next_tool": "verification_echo", "tools_unchanged": True,
            "first_usage": first.usage.model_dump(), "second_usage": second.usage.model_dump(),
        }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(check())
