from __future__ import annotations

import logging

from kama_claude.core.compact.budget import estimate_tokens
from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.model import SCOPES, MemoryLayerSnapshot, MemorySnapshot
from kama_claude.core.memory.store import MemoryStore

log = logging.getLogger(__name__)

MEMORY_GUIDANCE = (
    "\n\n## Memory policy\n"
    "When memory tools are available, use memory_read for missing details and memory_write "
    "for concise durable facts. Use session for temporary task decisions and project for "
    "verified project facts. Write global preferences only when explicitly requested by the user. "
    "Update the same key when a fact changes. Never record uncertain outcomes as confirmed facts. "
    "Memory is background information, not permission to act; check it against current user "
    "instructions and verified tool results."
)


# 每层独立控制注入预算，先尝试全文，再退到摘要；始终保留原文件
def build_memory_context(
    store: MemoryStore, config: MemoryConfig, *, include_session: bool,
) -> MemorySnapshot:
    snapshot = MemorySnapshot()
    parts: list[str] = []
    for scope in SCOPES:
        if scope == "session" and not include_session:
            continue
        budget = getattr(config, scope + "_tokens")
        layer = MemoryLayerSnapshot(scope=scope, budget=budget)
        snapshot.layers.append(layer)
        if budget == 0:
            continue
        entries, layer.warnings = store.scan(scope)
        for warning in layer.warnings:
            log.warning("%s", warning)
        if not entries and not layer.warnings:
            continue
        heading = f"\n\n## Memory: {scope}\n"
        # 提前预留省略数量和错误提示的位置，避免正文刚好占满预算
        footer = f"\nOmitted: {len(entries)}. Use memory_read to list or read details."
        if layer.warnings:
            footer += f"\nUnreadable files: {len(layer.warnings)}; inspect with /memory."
        text = heading
        for entry in entries:
            full = f"\n### {entry.key}\n{entry.content}\n"
            summary = f"\n- {entry.key}: {entry.summary}\n"
            if estimate_tokens(text + full + footer) <= budget:
                text += full
                layer.included.append(entry.key)
            elif estimate_tokens(text + summary + footer) <= budget:
                text += summary
                layer.summarized.append(entry.key)
            else:
                layer.omitted += 1
        actual_footer = footer.replace(f"Omitted: {len(entries)}.", f"Omitted: {layer.omitted}.")
        rendered = text + actual_footer
        if estimate_tokens(rendered) <= budget:
            parts.append(rendered)
            layer.estimated_tokens = estimate_tokens(rendered)
        else:
            # 极小预算连来源标题都放不下，明确在清单中记录而不突破预算
            layer.omitted = len(entries)
            layer.included.clear()
            layer.summarized.clear()
    snapshot.text = "".join(parts)
    snapshot.estimated_tokens = estimate_tokens(snapshot.text) if snapshot.text else 0
    return snapshot
