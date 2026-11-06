from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any, Protocol


class Embedder(Protocol):
    # 指纹同时区分模型文件和预处理约定。
    def fingerprint(self) -> str: ...

    # 工具文本使用 passage 前缀。
    def documents(self, texts: list[str]) -> list[list[float]]: ...

    # 背景与当前需求分配不同额度，避免长背景挤掉检索意图。
    def query(self, background: str, description: str) -> list[float]: ...


# 显式目录严格离线；未指定目录时首次下载官方模型，以后优先使用完整本地缓存。
def model_directory(configured_path: str) -> Path:
    if configured_path:
        path = Path(configured_path).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError("local embedding model directory is missing")
        return path
    hub = importlib.import_module("huggingface_hub")
    repo = "intfloat/multilingual-e5-small"
    cache = str(Path.home() / ".kama" / "models")
    try:
        path = Path(hub.snapshot_download(repo_id=repo, cache_dir=cache, local_files_only=True))
        required = (
            "model.safetensors", "config.json", "modules.json", "1_Pooling/config.json",
            "tokenizer.json", "tokenizer_config.json", "sentencepiece.bpe.model",
        )
        if all((path / name).is_file() for name in required):
            return path
    except FileNotFoundError:
        pass
    # 只下载一份权重和必要配置，不重复下载 bin、ONNX、OpenVINO 版本。
    return Path(hub.snapshot_download(
        repo_id=repo, cache_dir=cache, local_files_only=False,
        allow_patterns=["*.json", "*.model", "model.safetensors"],
        ignore_patterns=["onnx/*", "openvino/*", ".eval_results/*"],
    ))


class LocalE5Embedder:
    # 模型只加载一次，调用方负责串行化并移出事件循环。
    def __init__(self, model_path: str) -> None:
        self._path = model_path
        self._model: Any = None
        self._key = ""

    # 库负责模型下载与加载，应用只管理缓存指纹和查询、文档的预处理。
    def fingerprint(self) -> str:
        if self._key:
            return self._key
        # 依赖缺失时立即降级，避免每次搜索都先扫描大权重文件。
        module = importlib.import_module("sentence_transformers")
        path = model_directory(self._path)
        files = sorted(item for item in path.rglob("*") if item.is_file())
        if not files:
            raise RuntimeError("local embedding model directory is empty")
        hasher = hashlib.sha256(b"e5-query128-passage512-v1")
        # 每个 Core 生命周期只扫描一次，权重内容更新也会使缓存失效。
        for file in files:
            hasher.update(file.relative_to(path).as_posix().encode())
            with file.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
        self._model = module.SentenceTransformer(
            str(path), device="cpu", local_files_only=True, trust_remote_code=False,
        )
        self._model.max_seq_length = 512
        self._key = hasher.hexdigest()
        return self._key

    # E5 文档编码保留其训练要求的前缀，返回普通 Python 数值。
    def documents(self, texts: list[str]) -> list[list[float]]:
        self.fingerprint()
        encoded = self._model.encode(
            ["passage: " + text for text in texts], batch_size=16,
            normalize_embeddings=True, show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in encoded]

    # 为背景最多分配 128 Token，剩余优先用于描述，最终总长不超过模型限制。
    def query(self, background: str, description: str) -> list[float]:
        self.fingerprint()
        tokenizer = self._model.tokenizer
        context_ids = tokenizer.encode(background, add_special_tokens=False)[:128]
        prefix = "query: 任务背景：" + tokenizer.decode(context_ids) + "\n当前需求："
        overhead = len(tokenizer.encode(prefix, add_special_tokens=False))
        detail_ids = tokenizer.encode(description, add_special_tokens=False)
        detail_ids = detail_ids[:max(1, 508 - overhead)]
        encoded = self._model.encode(
            [prefix + tokenizer.decode(detail_ids)], normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [float(x) for x in encoded[0]]
