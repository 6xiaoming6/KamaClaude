from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest
from PIL import Image

from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.estimator import TokenEstimator, estimate_bytes
from kama_claude.core.compact.history import validate_pairs
from kama_claude.core.compact.output import ToolOutputPreparer
from kama_claude.core.config import CompactionConfig, KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.images import IMAGE_TOKEN_ESTIMATE, supports_images, validate_images
from kama_claude.core.llm.managed import ManagedProvider
from kama_claude.core.llm.provider import AnthropicProvider, DeepseekProvider, OpenaiProvider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock
from kama_claude.core.runner import AgentRunner
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.workspace import WorkspacePathError
from kama_claude.tui.conversation import ConversationView
from tests.unit.discovery_support import ScriptedProvider
from tests.unit.test_compactor import history, provider


def picture(tmp_path, suffix="png", size=(32, 16)):
    path = tmp_path / f"image.{suffix}"
    Image.new("RGB", size, "red").save(path)
    return path


def image_history(result):
    return [
        {"role": "user", "content": "What color is this image?"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "image", "name": "read_file", "input": {"path": "x.png"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "image", "content": result.message_content()},
        ]},
    ]


# 真正解码返回的图片，避免只检查工具声称“附带图片”的文字。
@pytest.mark.parametrize("suffix", ["png", "jpg", "webp", "gif"])
async def test_supported_formats_are_attached_as_real_images(tmp_path, suffix):
    path = picture(tmp_path, suffix)
    result = await ReadFileTool(tmp_path).invoke({"path": path.name})
    source = result.images[0]["source"]
    with Image.open(io.BytesIO(base64.b64decode(source["data"]))) as sent:
        assert sent.size == (32, 16)
        assert sent.convert("RGB").getpixel((0, 0))[0] > 240
    assert source["media_type"] == "image/png"
    assert source["data"] not in result.content
    assert result.message_content()[-1] == result.images[0]


async def test_resize_and_exif_orientation(tmp_path):
    path = tmp_path / "oriented.jpg"
    image = Image.new("RGB", (2000, 1000), "red")
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)
    result = await ReadFileTool(tmp_path).invoke({"path": path.name})
    with Image.open(io.BytesIO(base64.b64decode(result.images[0]["source"]["data"]))) as sent:
        assert sent.size == (768, 1536)


async def test_gif_uses_first_frame(tmp_path):
    path = tmp_path / "animated.gif"
    Image.new("RGB", (8, 8), "red").save(
        path, save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")], duration=10,
    )
    result = await ReadFileTool(tmp_path).invoke({"path": path.name})
    assert "first frame only" in result.content
    with Image.open(io.BytesIO(base64.b64decode(result.images[0]["source"]["data"]))) as sent:
        assert sent.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


async def test_image_magic_not_only_extension_and_text_paging_unchanged(tmp_path):
    path = picture(tmp_path).rename(tmp_path / "screenshot.data")
    assert (await ReadFileTool(tmp_path).invoke({"path": path.name})).images
    text = tmp_path / "source.py"
    text.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = await ReadFileTool(tmp_path).invoke({"path": text.name, "start_line": 2, "max_lines": 1})
    assert not result.images and "2 | two" in result.content and "next_start_line: 3" in result.content


@pytest.mark.parametrize("case", ["corrupt", "bytes", "pixels", "paging", "model"])
async def test_invalid_images_fail_before_model_request(tmp_path, monkeypatch, case):
    path = picture(tmp_path)
    params = {"path": path.name}
    if case == "corrupt":
        path.write_bytes(b"not an image")
    elif case == "bytes":
        monkeypatch.setattr("kama_claude.core.tools.images.MAX_IMAGE_BYTES", 10)
    elif case == "pixels":
        monkeypatch.setattr("kama_claude.core.tools.images.MAX_IMAGE_PIXELS", 10)
    elif case == "paging":
        params["max_lines"] = 1
    with pytest.raises(ToolError) as error:
        await ReadFileTool(tmp_path, image_support=case != "model").invoke(params)
    assert error.value.error_type == ("resource_limit" if case in {"bytes", "pixels"}
                                      else "invalid_operation")


async def test_external_image_still_needs_backend_path_approval(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    image = picture(tmp_path)
    tool = ReadFileTool(root)
    with pytest.raises(WorkspacePathError):
        await tool.invoke({"path": str(image)})
    result = await tool.invoke({"path": str(image)}, approved_path=image.resolve())
    assert result.images
    # 内部授权不是模型可以提供的工具参数。
    with pytest.raises(ValueError):
        await tool.invoke({"path": str(image), "approved_path": str(image)})


async def test_output_preparation_and_ui_do_not_crop_or_display_base64(tmp_path):
    result = await ReadFileTool(tmp_path).invoke({"path": picture(tmp_path).name})
    frozen = deepcopy(result.images)
    result.content += " detailed metadata" * 100
    prepared = ToolOutputPreparer(
        CompactionConfig(tool_result_limit=100, tool_result_keep=40), tmp_path,
    ).prepare("image", result)
    assert prepared[-1] == frozen[0] and result.images == frozen
    assert "truncated" in prepared[0]["text"]
    assert "base64" not in ConversationView._tool_result_text(prepared)
    assert ToolResult(**asdict(result)).images == frozen


@pytest.mark.parametrize("model, supported", [
    ("deepseek-flash", True), ("deepseek-v4-pro", False),
    ("claude-sonnet-4-6", True), ("claude-opus-4-7", False),
])
def test_deepseek_model_capability(model, supported):
    assert supports_images("deepseek", model) is supported


async def test_unsupported_history_fails_before_budget_or_sdk(tmp_path):
    result = await ReadFileTool(tmp_path).invoke({"path": picture(tmp_path).name})
    inner = AsyncMock()
    managed = ManagedProvider(inner, TokenEstimator("deepseek", "deepseek-v4-pro"), CompactionConfig())
    with pytest.raises(ValueError, match="does not support"):
        await managed.chat(image_history(result), [], EventBus(), "r")
    inner.chat.assert_not_called()
    with pytest.raises(ValueError, match="image request limit"):
        validate_images(image_history(ToolResult("many", images=result.images * 21)))


async def test_openai_finishes_all_tool_responses_before_image_message(tmp_path):
    result = await ReadFileTool(tmp_path).invoke({"path": picture(tmp_path).name})
    messages = image_history(result)
    messages[1]["content"].append({"type": "tool_use", "id": "other", "name": "read_file", "input": {}})
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "other", "content": "text file"},
    ]})
    original = deepcopy(messages)
    converted = OpenaiProvider("vision", client=object())._convert_messages(messages)
    assert [m["role"] for m in converted] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert converted[3]["tool_call_id"] == "image" and isinstance(converted[3]["content"], str)
    assert converted[-1]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert messages == original


# 使用真实 SDK 序列化到 HTTP 边界，网络由 MockTransport 接管，不调用收费模型。
@pytest.mark.parametrize("provider_type", [AnthropicProvider, DeepseekProvider])
async def test_sdk_serializes_nested_tool_image_without_mutating_history(tmp_path, provider_type):
    result = await ReadFileTool(tmp_path).invoke({"path": picture(tmp_path).name})
    messages = image_history(result)
    original = deepcopy(messages)
    requests = []
    events = [
        ("message_start", {"message": {"id": "msg", "type": "message", "role": "assistant",
         "model": "vision", "content": [], "stop_reason": None, "stop_sequence": None,
         "usage": {"input_tokens": 30, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "red"}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {}),
    ]
    body = "".join(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"
                   for kind, data in events)

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    async with anthropic.AsyncAnthropic(api_key="test-only", http_client=http) as client:
        response = await provider_type("deepseek-flash", client=client).chat(
            messages, [], EventBus(), "r",
        )
    assert response.text == "red" and messages == original
    assert requests[0]["messages"][-1]["content"][0]["content"][-1] == result.images[0]


def test_image_estimate_does_not_count_base64_as_text_and_keeps_business_params():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    short = estimate_bytes(image)
    image["source"]["data"] = "x" * 100_000
    assert estimate_bytes(image) == short >= IMAGE_TOKEN_ESTIMATE
    business = {"type": "tool_use", "input": image}
    assert estimate_bytes(business) > short


async def test_real_usage_anchor_includes_image_and_invalidates_on_change(tmp_path):
    result = await ReadFileTool(tmp_path).invoke({"path": picture(tmp_path).name})
    messages = image_history(result)
    estimator = TokenEstimator("deepseek", "deepseek-flash")
    estimator.observe("sys", messages, [], 800, output_tokens=10)
    tail = {"role": "user", "content": "continue"}
    counter = estimator.select("sys", [*messages, tail], [])
    assert counter.method == "usage_anchor"
    assert counter.request("sys", [*messages, tail], []) == 810 + estimate_bytes([tail])
    messages[-1]["content"][0]["content"][-1]["source"]["data"] = "changed"
    assert estimator.select("sys", messages, []).method == "bytes"


# 工具结果已提交后源文件被删除；恢复仍使用原图片快照，不能重读或重跑工具。
async def test_runner_persists_image_and_restores_without_source_file(tmp_path, monkeypatch):
    path = picture(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-image", "chat", "active", "", "now", "now")
    first = ScriptedProvider([
        LlmResponse("tool_use", tool_calls=[ToolCallBlock("image", "read_file", {"path": str(path)})]),
        RuntimeError("connection interrupted"),
    ])
    # 固定 workspace 不依赖测试启动目录。
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(KamaConfig(), provider=first)
    outcome = await runner.run_and_capture("inspect image", session=session, store=store)
    assert outcome.status == "interrupted"
    cp = store.ensure_checkpoint(session.id)
    frozen = deepcopy(cp.messages[-1]["content"][0]["content"][-1])
    assert frozen["type"] == "image"
    assert store.read_history(session.id)[-1]["content"][0]["content"][-1] == frozen
    path.unlink()
    resumed = ScriptedProvider([LlmResponse("end_turn", text="red")])
    outcome = await AgentRunner(KamaConfig(), provider=resumed).run_and_capture(
        "inspect image", session=session, store=store, checkpoint=cp,
    )
    assert outcome.status == "success"
    assert resumed.requests[0]["messages"][-1]["content"][0]["content"][-1] == frozen


async def test_compaction_keeps_recent_image_and_can_summarize_older_image(tmp_path):
    result = await ReadFileTool(tmp_path).invoke({"path": picture(tmp_path).name})
    recent = image_history(result)
    compactor = Compactor(CompactionConfig(keep_recent_turns=1))
    compacted = await compactor.compact_messages([*history(), *recent], provider())
    assert compacted.compacted and compacted.messages[-3:] == recent
    assert validate_pairs(compacted.messages)
    compacted = await compactor.compact_messages([*recent, *history()], provider())
    assert compacted.compacted and '"type": "image"' not in json.dumps(compacted.messages)
