from __future__ import annotations

from typing import Any

from textual.widgets import Static


class ReplyUsage(Static):
    # 一次模型回复一条显示；完整 Usage 到达后替换流式阶段的估算。
    def __init__(self) -> None:
        super().__init__(markup=False, classes="usage")
        self.input_tokens: int | None = None
        self.output_tokens = 0
        self.output_bytes = 0
        self.window_tokens: int | None = None
        self.cache_hit_rate: float | None = None
        self.complete = False

    @property
    def context_pct(self) -> float | None:
        if self.input_tokens is None or not self.window_tokens:
            return None
        return (self.input_tokens + self.output_tokens) / self.window_tokens

    def append_output(self, event: dict[str, Any]) -> None:
        if event.get("input_tokens_estimate") is not None:
            self.input_tokens = event["input_tokens_estimate"]
        if event.get("context_window_tokens") is not None:
            self.window_tokens = event["context_window_tokens"]
        # 累加字节后统一取整，避免片段切分方式改变估算量。
        self.output_bytes += len(str(event.get("token", "")).encode("utf-8"))
        self.output_tokens = (self.output_bytes + 3) // 4
        self._update_text()

    def finish(self, event: dict[str, Any]) -> None:
        total = event.get("total_input_tokens")
        self.input_tokens = int(event.get("input_tokens", 0) if total is None else total)
        self.output_tokens = int(event.get("output_tokens", 0))
        self.window_tokens = event.get("context_window_tokens") or self.window_tokens
        # 旧事件没有窗口字段时，从其输入占比推算；没有依据就显示未知。
        if not self.window_tokens and self.input_tokens and event.get("context_pct"):
            self.window_tokens = round(self.input_tokens / float(event["context_pct"]))
        cache_read = int(event.get("cache_read_input_tokens", 0))
        self.cache_hit_rate = cache_read / self.input_tokens if self.input_tokens else None
        self.complete = True
        self._update_text()

    def _update_text(self) -> None:
        prefix = "" if self.complete else "≈"
        input_text = "—" if self.input_tokens is None else f"{prefix}{self.input_tokens:,}"
        cache = "—" if self.cache_hit_rate is None else f"{self.cache_hit_rate:.1%}"
        pct = self.context_pct
        context = "—" if pct is None else f"{prefix}{pct:.1%}"
        self.update(
            f"输入 {input_text} · 输出 {prefix}{self.output_tokens:,}"
            f" · 缓存命中 {cache} · 上下文 {context}"
        )
