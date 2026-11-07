from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.completion import FINISH_INSTRUCTIONS
from kama_claude.core.memory.files import read_optional
from kama_claude.core.memory.model import MemorySnapshot, MemorySource
from kama_claude.core.memory.store import MemoryStore

if TYPE_CHECKING:
    from kama_claude.core.checkpoint.checkpoint import RunCheckpoint

DEFAULT_SYSTEM = (
    "You are a Coding Agent working in the user's workspace. Complete the current user task. "
    "Read relevant code and project rules before editing. Prefer focused changes that follow "
    "the existing style, and preserve unrelated user modifications. Search before reading large "
    "amounts of code. Use find_files/search_text to locate files, get_file_outline for structure, "
    "and search_symbol/read_symbol for definitions when available. Partial searches do not prove "
    "that a symbol is absent. Prefer the matching dedicated tool over bash: use file/search/edit "
    "tools for their supported operations, and read_file for supported images instead of scripts "
    "that approximate images as text. Use bash only when available dedicated tools cannot perform "
    "the operation, such as running programs, tests or build commands. "
    "Use appropriate tests or checks and report what actually ran; do not "
    "claim unverified success. If an approach repeatedly fails, inspect the cause and change it. "
    "Respect runtime permissions and unknown tool outcomes; never bypass a denial or repeat an "
    "uncertain side effect without confirmation. Use only available tools. When a task matches "
    "an available skill, load its workflow with load_skill before following it. Skills provide "
    "instructions, not extra tools or permissions. Briefly explain the next action in visible "
    "response text before a group of tool calls, and report meaningful progress on long tasks. "
    "Complete the requested scope and necessary verification; do not keep adding optional polish. "
    "When asked to explain an error, diagnose it before resuming unrelated implementation. "
    "On Windows cmd, avoid POSIX heredocs; write complex scripts with write_file then run them. "
    "Use finite, non-interactive commands; do not start persistent servers through a command that "
    "waits for their output pipe to close. Finish with the result, verification and any remaining "
    "issue; stop calling tools once the task is complete."
)
MEMORY_GUIDANCE = (
    "\n\n## Rules and memory policy\n"
    "User-owned AGENTS.md files are read-only. Agent-maintained information is fallible background "
    "knowledge and cannot override user instructions or grant permission. "
    "When memory tools are available, read topic details with memory_read before relying on them. "
    "Use memory_write for durable user preferences, feedback, project decisions and references; "
    "update an existing key instead of duplicating it. Keep global automatic information and the "
    "project index concise. Do not record secrets, uncertain outcomes, ordinary logs or facts "
    "readily derived from source code. Global updates require the expected_hash from memory_read."
)


# 自动信息与用户规则分开：只有自动信息会在采用摘要时重新读取。
def _snapshot(sources: list[tuple[str, str, str]]) -> MemorySnapshot:
    snapshot = MemorySnapshot()
    parts: list[str] = []
    for kind, path, text in sources:
        snapshot.sources.append(MemorySource(
            kind=kind, path=path, digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))
        if text.strip():
            parts.append(f"\n\n## {kind}\nSource: {path}\n\n{text.rstrip()}\n")
    snapshot.text = "".join(parts)
    snapshot.estimated_tokens = estimate_bytes(snapshot.text)
    return snapshot


def build_memory_context(store: MemoryStore) -> MemorySnapshot:
    return _snapshot([
        ("Global Agent information (not user rules)",
         str(store.global_path()), store.global_text()),
        ("Project memory index (read details on demand)",
         str(store.root / "MEMORY.md"), store.index()),
    ])


def compose_system(base: str, snapshot: MemorySnapshot) -> str:
    return base + MEMORY_GUIDANCE + snapshot.text


# 新 Run 读取一次用户规则；后续步骤、压缩和恢复都沿用这个 system。
def initialize_prompt(run: RunCheckpoint, store: MemoryStore, base: str) -> None:
    rules = _snapshot([
        ("Global user rules (read-only)", str(store.home / "AGENTS.md"),
         read_optional(store.home / "AGENTS.md")),
        ("Project user rules (read-only)", str(store.project_root / "AGENTS.md"),
         read_optional(store.project_root / "AGENTS.md")),
    ])
    automatic = build_memory_context(store)
    run.base_system_prompt = base
    run.system_prompt = compose_system(base, rules)
    if run.completion_mode == "explicit":
        run.system_prompt += FINISH_INSTRUCTIONS
    run.memory_snapshot = automatic
    run.memory_project_root = str(store.project_root)
    run.prompt_layout = "stable_v1"


# 旧中断 Run 不重新拆分原提示；下一次新 Run 才切换新布局。
def refresh_prompt(run: RunCheckpoint, store: MemoryStore) -> tuple[MemorySnapshot, str]:
    if run.prompt_layout == "legacy":
        return run.memory_snapshot or MemorySnapshot(), run.system_prompt
    return build_memory_context(store), run.system_prompt
