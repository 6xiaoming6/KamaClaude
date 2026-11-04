from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from kama_claude.core.checkpoint.checkpoint import UsageAnchor


# 文本按 UTF-8 字节数除以四向上取整；消息先规范序列化，计入结构字段
def estimate_bytes(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return (len(text.encode("utf-8")) + 3) // 4


# 指纹同时覆盖系统提示、工具定义和已统计的消息前缀，检测历史原地修改
def _prefix_hash(system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]) -> str:
    text = json.dumps([system, schemas, messages], ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 只有上次请求的完整输入仍是当前前缀，实际 Usage 才能继续作为锚点
def _matches(
    anchor: UsageAnchor, system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
) -> bool:
    return len(messages) >= anchor.message_count and anchor.prefix_hash == _prefix_hash(
        system, messages[:anchor.message_count], schemas,
    )


@dataclass(frozen=True)
class TokenCounter:
    anchor: UsageAnchor | None = None
    underestimate_factor: float = 1.0

    # 片段和压缩前后比较统一使用字节粗估，不给实际 Usage 再乘校准系数
    def count(self, value: Any) -> int:
        return estimate_bytes(value)

    # 全文粗估用于首次请求、锚点失效和 Cline 式低估比例
    def raw_request(
        self, system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
    ) -> int:
        return self.count({"system": system, "messages": messages, "tools": schemas})

    # 上次输入 Usage 不包含上次回复，回复和新增工具结果都会在尾部估算一次
    def request(
        self, system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
    ) -> int:
        if self.anchor is not None and _matches(self.anchor, system, messages, schemas):
            tail = messages[self.anchor.message_count:]
            return self.anchor.input_tokens + (self.count(tail) if tail else 0)
        return self.raw_request(system, messages, schemas)

    @property
    # 标识本次选择的估算来源，供预算事件展示
    def method(self) -> str:
        return "usage_anchor" if self.anchor is not None else "bytes"

    # 按 Cline 的有界低估比例缩小预算，不增加原模型可用空间
    def effective_limit(self, input_limit: int) -> int:
        return math.floor(input_limit / self.underestimate_factor)

    # 每次准备请求重新计算，项目默认压缩比例仍为百分之八十
    def trigger(self, input_limit: int, ratio: float) -> int:
        return math.floor(input_limit / self.underestimate_factor * ratio)


class TokenEstimator:
    # 每种 Provider、模型和请求用途仅保存最新有效锚点，直接绑定检查点数据
    def __init__(
        self, provider: str = "", model: str = "",
        anchors: dict[str, UsageAnchor] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.anchors = anchors if anchors is not None else {}

    # 普通请求与摘要隔离，切换模型或估算版本不会混用旧 Usage
    def _key(self, kind: str) -> str:
        return json.dumps(["bytes-v1", self.provider, self.model, kind], separators=(",", ":"))

    # 先检查前缀，再用上次实际输入与本次全文粗估重新计算有效预算
    def select(
        self, system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
        *, kind: str = "normal",
    ) -> TokenCounter:
        anchor = self.anchors.get(self._key(kind))
        if anchor is None or not _matches(anchor, system, messages, schemas):
            return TokenCounter()
        rough = TokenCounter().raw_request(system, messages, schemas)
        factor = min(4.0, max(1.0, anchor.input_tokens / max(1, rough)))
        return TokenCounter(anchor, factor)

    # 仅记录完整响应中的有效总输入；失败和缺失 Usage 不覆盖旧锚点
    def observe(
        self, system: str, messages: list[dict[str, Any]], schemas: list[dict[str, Any]],
        actual_input: int, *, kind: str = "normal",
    ) -> None:
        if type(actual_input) is not int or actual_input <= 0:
            return
        self.anchors[self._key(kind)] = UsageAnchor(
            input_tokens=actual_input, message_count=len(messages),
            prefix_hash=_prefix_hash(system, messages, schemas),
        )
        # 限制长期切换模型时的元数据量，不另设全局统计服务
        if len(self.anchors) > 32:
            del self.anchors[next(iter(self.anchors))]
