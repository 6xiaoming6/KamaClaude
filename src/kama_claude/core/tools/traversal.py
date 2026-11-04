from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from pathspec import GitIgnoreSpec

from kama_claude.core.tools.files import ReadBudget, check_target

_IGNORED = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "runs",
})
_MAX_SCAN_ENTRIES = 20_000


# 目录链接只显示或跳过，不递归跟随符号链接和 Windows junction
def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


# 按路径片段匹配 glob；** 可以匹配零层，单文件名模式搜索所有层级
def matches_glob(relative: str, pattern: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    patterns = pattern.replace("\\", "/").split("/")
    if len(patterns) == 1:
        patterns.insert(0, "**")

    @cache
    # 缓存下标组合，避免多个 ** 导致指数级回溯
    def match(i: int, j: int) -> bool:
        if i == len(patterns):
            return j == len(parts)
        if patterns[i] == "**":
            return match(i + 1, j) or (j < len(parts) and match(i, j + 1))
        return j < len(parts) and fnmatch.fnmatchcase(parts[j], patterns[i]) and match(i + 1, j + 1)

    return match(0, 0)


class DirectoryScanner:
    # 一次调用内缓存各级 ignore 规则，目录条目、文本读取共享超时
    def __init__(
        self, root: Path, workspace: Path, include_ignored: bool, budget: ReadBudget,
    ) -> None:
        self.root = root
        self.base = workspace if root.is_relative_to(workspace) else root
        self.include_ignored = include_ignored
        self.budget = budget
        self.visited = 0
        self.skipped = 0
        self.reason = ""
        self._specs: dict[Path, GitIgnoreSpec] = {}

    # 规则文件自身不能通过链接读取外部内容，最多读取 64 KiB
    def _spec(self, directory: Path) -> GitIgnoreSpec:
        if directory not in self._specs:
            ignore = directory / ".gitignore"
            lines: list[str] = []
            if ignore.is_file() and not is_link(ignore):
                check_target(ignore)
                with ignore.open("r", encoding="utf-8", errors="replace") as stream:
                    lines = stream.read(65_536).splitlines()
            self._specs[directory] = GitIgnoreSpec.from_lines(lines)
        return self._specs[directory]

    # 先处理常见生成目录，再按从父到子的 .gitignore 规则评估
    def _ignored(self, path: Path) -> bool:
        if self.include_ignored:
            return False
        if path.name in _IGNORED and (path.name == ".git" or path.is_dir()):
            return True
        ignored = False
        parents = [*reversed(path.parent.relative_to(self.base).parents),
                   path.parent.relative_to(self.base)]
        for relative in parents:
            directory = self.base / relative
            pattern = path.relative_to(directory).as_posix()
            if not is_link(path) and path.is_dir():
                pattern += "/"
            match = self._spec(directory).check_file(pattern)
            if match.include is not None:
                ignored = match.include
        return ignored

    # 有界地收集目录条目，过滤后排序，避免一次排序读取任意数量的文件名
    def entries(self, directory: Path) -> list[Path]:
        check_target(directory)
        entries: list[Path] = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if not self.budget.check():
                    break
                self.visited += 1
                if self.visited > _MAX_SCAN_ENTRIES:
                    self.reason = "scan_entries_limit"
                    break
                path = Path(entry.path)
                if not self._ignored(path):
                    entries.append(path)
        return sorted(entries, key=lambda item: (not is_link(item) and item.is_file(), item.name))

    # 深度优先扫描普通文件，链接不读取；目录过深时明确报告截断
    def files(self) -> Iterator[Path]:
        stack = [(self.root, 0)]
        while stack and self.budget.check() and not self.reason:
            directory, depth = stack.pop()
            if depth > 64:
                self.budget.reason = "directory_depth_limit"
                return
            try:
                entries = self.entries(directory)
            except (PermissionError, FileNotFoundError):
                if directory == self.root:
                    raise
                self.skipped += 1
                continue
            directories: list[Path] = []
            for path in entries:
                if not self.budget.check():
                    return
                if is_link(path):
                    continue
                if path.is_dir():
                    directories.append(path)
                elif path.is_file():
                    yield path
            stack.extend((path, depth + 1) for path in reversed(directories))
