from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime
from typing import Any

import anthropic

from kama_claude.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(self, model: str, client: Any = None) -> None:
        if client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise SystemExit("ANTHROPIC_API_KEY not set")
            self._client: Any = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            self._client = client
        self._model = model

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse
    # 历史消息和工具描述由外部的agent_loop通过messages和tool_schemas传递
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system: list[dict[str, object]] = [
            {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        ]

        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []

        # 流式返回模型输出，每收到一段就发布一个LlmTokenEvent
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                text_parts.append(text)
            # 取得 SDK 汇总后的完整响应
            final_message = await stream.get_final_message()

        # 提取并发布token用量
        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0

        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                ts=_now(),
            )
        )

        # 从完整响应中提取工具调用
        tool_calls: list[ToolCallBlock] = []
        for block in final_message.content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCallBlock(id=block.id, name=block.name, input=dict(block.input))
                )

        # 把模型的文字回复和工具调用单独拆分出来，包装成LlmResponse返回
        return LlmResponse(
            stop_reason=final_message.stop_reason or "end_turn",
            tool_calls=tool_calls,
            text="".join(text_parts),
            usage=UsageStats(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
            ),
        )


class OpenaiProvider:
    # 初始化 OpenAI 客户端；延迟加载 SDK，注入测试客户端时无需安装或提供密钥
    def __init__(self, model: str, client: Any = None) -> None:
        if client is None:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise SystemExit("OPENAI_API_KEY not set")
            try:
                sdk = importlib.import_module("openai")
            except ModuleNotFoundError as exc:
                if exc.name != "openai":
                    raise
                raise SystemExit("OpenaiProvider requires the openai package") from exc
            self._client: Any = sdk.AsyncOpenAI(api_key=api_key)
        else:
            self._client = client
        self._model = model

    # 将现有上下文中的文本、tool_use 和 tool_result 转换为 OpenAI 消息
    def _convert_messages(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        converted: list[dict[str, object]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        for message in messages:
            role, content = message["role"], message["content"]
            if isinstance(content, str):
                converted.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                raise ValueError("Message content must be text or a list of content blocks")

            texts: list[str] = []
            calls: list[dict[str, object]] = []
            for block in content:
                if block["type"] == "text":
                    texts.append(block["text"])
                elif block["type"] == "tool_use" and role == "assistant":
                    calls.append(
                        {
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block["input"], ensure_ascii=False),
                            },
                        }
                    )
                elif block["type"] == "tool_result" and role == "user":
                    if texts:
                        converted.append({"role": role, "content": "".join(texts)})
                        texts.clear()
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        }
                    )
                else:
                    raise ValueError(f"Unsupported {role} content block: {block['type']}")

            if role == "assistant":
                assistant: dict[str, object] = {"role": role, "content": "".join(texts) or None}
                if calls:
                    assistant["tool_calls"] = calls
                converted.append(assistant)
            elif texts:
                converted.append({"role": role, "content": "".join(texts)})
        return converted

    # 流式调用 OpenAI，转换工具协议并发布与 Anthropic 相同的模型、文本和用量事件
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )
        kwargs: dict[str, object] = {
            "model": self._model,
            "max_completion_tokens": 4096,
            "messages": self._convert_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tool_schemas:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": schema["name"],
                        "description": schema.get("description", ""),
                        "parameters": schema["input_schema"],
                    },
                }
                for schema in tool_schemas
            ]

        texts: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        usage: UsageStats | None = None
        finish_reason: str | None = None
        stream = await self._client.chat.completions.create(**kwargs)
        async with stream:
            async for chunk in stream:
                if chunk.usage is not None:
                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    usage = UsageStats(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                        cache_read_input_tokens=getattr(details, "cached_tokens", 0) or 0,
                    )
                for choice in chunk.choices:
                    if choice.index != 0:
                        continue
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta.content:
                        texts.append(delta.content)
                        await bus.publish(
                            LlmTokenEvent(run_id=run_id, token=delta.content, ts=_now())
                        )
                    for fragment in delta.tool_calls or []:
                        call = pending.setdefault(
                            fragment.index,
                            {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            },
                        )
                        call["id"] += fragment.id or ""
                        if fragment.function is not None:
                            call["name"] += fragment.function.name or ""
                            call["arguments"] += fragment.function.arguments or ""

        if usage is not None:
            await bus.publish(
                LlmUsageEvent(
                    run_id=run_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    ts=_now(),
                )
            )
        if finish_reason not in {"stop", "tool_calls"}:
            raise ValueError(f"Incomplete OpenAI response: finish_reason={finish_reason!r}")
        if bool(pending) != (finish_reason == "tool_calls"):
            raise ValueError("Tool calls do not match the OpenAI finish_reason")

        tool_calls: list[ToolCallBlock] = []
        for index in sorted(pending):
            call = pending[index]
            if not call["id"] or not call["name"]:
                raise ValueError("Tool call is missing its id or function name")
            arguments = json.loads(call["arguments"] or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("Tool call arguments must be a JSON object")
            tool_calls.append(ToolCallBlock(id=call["id"], name=call["name"], input=arguments))

        return LlmResponse(
            stop_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls,
            text="".join(texts),
            usage=usage,
        )
