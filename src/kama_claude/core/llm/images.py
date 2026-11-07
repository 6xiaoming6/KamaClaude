from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# 经过本地尺寸限制后的保守占位估算；收到真实 Usage 后继续沿用原有锚点。
IMAGE_TOKEN_ESTIMATE = 4096


def supports_images(provider: str, model: str) -> bool:
    if provider != "deepseek":
        return True  # 其他后端需配置支持视觉的模型；不根据模型名字猜测厂商。
    name = model.lower()
    return (name in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
            or name.startswith("deepseek-v4.1-flash")
            or (name.startswith("claude") and not name.startswith("claude-opus")))


def image_blocks(messages: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "image":
                yield block
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                yield from (item for item in block["content"] if item.get("type") == "image")


def validate_images(messages: list[dict[str, Any]], *, supported: bool = True) -> None:
    count = size = 0
    for block in image_blocks(messages):
        if not supported:
            raise ValueError("current model does not support image input; use a vision model")
        source = block.get("source", {})
        if source.get("type") != "base64" or not isinstance(source.get("data"), str):
            raise ValueError("only inline base64 image snapshots are supported")
        count += 1
        size += len(source["data"])
    if count > 20 or size > 24 * 1024 * 1024:
        raise ValueError("image request limit exceeded (20 images/24 MiB encoded); compact history")


# Base64 是传输编码，不能作为普通文字按长度计算 Token；工具参数仍是业务数据。
def image_estimate_projection(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        if value.get("type") == "tool_use":
            return value, 0
        if value.get("type") == "image" and isinstance(value.get("source"), dict):
            source = value["source"]
            if source.get("type") == "base64":
                projected_image = {"type": "image", "media_type": source.get("media_type")}
                return projected_image, IMAGE_TOKEN_ESTIMATE
        projected = {}
        total = 0
        for key, item in value.items():
            projected[key], tokens = image_estimate_projection(item)
            total += tokens
        return projected, total
    if isinstance(value, list):
        items = [image_estimate_projection(item) for item in value]
        return [item for item, _ in items], sum(tokens for _, tokens in items)
    return value, 0


def openai_image(block: dict[str, Any]) -> dict[str, Any]:
    source = block["source"]
    return {"type": "image_url", "image_url": {
        "url": f"data:{source['media_type']};base64,{source['data']}",
    }}
