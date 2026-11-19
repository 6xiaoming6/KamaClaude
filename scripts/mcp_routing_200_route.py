"""200-tool routing endpoint: score and execute the first business call only."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import jsonschema
from benchmark_mcp_routing import (
    SYSTEM,
    CatalogTool,
    GetToolSchema,
    McpServerManager,
    SearchToolCandidates,
    ToolRetriever,
    callable_names,
    make_run,
    request_schemas,
    save,
)


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
        if not calls or (len(calls) > 1 and not all(
            block.name in business and block.name in allowed for block in calls
        )):
            row.update(status="invalid_call_count", call_count=len(calls))
            break
        if len(calls) > 1:
            # Endpoint is the first business call, not completion of every requested action.
            row["emitted_business_calls"] = [
                {"id": block.id, "name": block.name, "arguments": block.input}
                for block in calls
            ]
            row["unexecuted_additional_calls"] = len(calls)-1
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
