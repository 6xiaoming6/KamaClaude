from __future__ import annotations

import base64
import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.errors import ToolError
from kama_claude.core.tools.files import check_target

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PREPARED_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_SIDE = 1536
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


# 同时看文件头，避免把真实 PNG 当 UTF-8 解码；扩展名只作为损坏图片的识别线索。
def is_image(path: Path) -> bool:
    check_target(path)
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        head = stream.read(16)
    return (path.suffix.lower() in IMAGE_SUFFIXES or head.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
    ) or (head.startswith(b"RIFF") and head[8:12] == b"WEBP"))


def read_image(path: Path) -> ToolResult:
    check_target(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    check_target(path)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ToolError("image exceeds the 5 MiB input limit", "resource_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise ToolError("supported images: PNG, JPEG, WebP and GIF")
                width, height = original.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise ToolError("image exceeds the 20 megapixel limit", "resource_limit")
                animated = getattr(original, "is_animated", False)
                # 首帧快照、纠正 EXIF 方向；不执行图片中的任何外部内容。
                original.seek(0)
                image = ImageOps.exif_transpose(original).convert("RGBA")
                image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
        media_type = "image/png"
        if output.tell() > MAX_PREPARED_BYTES:
            # 高熵图片转 JPEG；透明背景合成到白底，避免直接丢 alpha 变成黑色。
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            output = io.BytesIO()
            background.save(output, format="JPEG", quality=80)
            media_type = "image/jpeg"
        if output.tell() > MAX_PREPARED_BYTES:
            raise ToolError("prepared image exceeds 1 MiB; use a smaller image", "resource_limit")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ToolError("image pixel count exceeds safe decoding limits", "resource_limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ToolError("invalid or damaged image") from exc
    note = "; first frame only" if animated else ""
    return ToolResult(
        content=(f"Image: {path}\nOriginal: {width}x{height}; sent: {image.width}x{image.height}; "
                 f"{media_type}; {output.tell()} bytes{note}. "
                 "The image is attached as visual input, not text."),
        images=[{"type": "image", "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.b64encode(output.getvalue()).decode("ascii"),
        }}],
    )
