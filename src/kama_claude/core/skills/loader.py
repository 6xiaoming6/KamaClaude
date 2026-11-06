from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.skills.model import NAME_PATTERN, SkillEntry, SkillError, SkillSnapshot

CATALOG_TOKENS = 2_048
BODY_TOKENS = 4_000


def _resolved(path: Path) -> Path:
    resolved = str(path.resolve())
    # Windows 并发创建文件时 resolve 可能保留长路径前缀；比较前统一表示。
    if os.name == "nt":
        if resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif resolved.startswith("\\\\?\\") and resolved[5:7] == ":\\":
            resolved = resolved[4:]
    return Path(resolved)


class SkillLoader:
    # 运行时唯一目录；包内文件只作为首次补齐的示例源。
    def __init__(self, root: Path | None = None) -> None:
        self.root = _resolved((root or Path.home() / ".kama" / "skills").expanduser())

    def _path(self, name: str) -> Path:
        if len(name) > 64 or re.fullmatch(NAME_PATTERN, name) is None:
            raise SkillError("invalid skill name: use 1-64 lowercase letters, digits and hyphens")
        try:
            path = _resolved(self.root / name / "SKILL.md")
            if not path.is_relative_to(self.root):
                raise SkillError(f"skill path escapes global skills directory: {name}")
            return path
        except (OSError, RuntimeError) as exc:
            raise SkillError(f"cannot resolve skill {name}: {exc}") from exc

    def ensure_examples(self) -> None:
        marker = self.root / ".examples-initialized"
        if os.path.lexists(marker):
            return
        # 先写完整临时文件，再以硬链接原子创建目标；并发初始化也不会覆盖用户内容。
        for source in sorted((Path(__file__).parent / "builtin").glob("*.md")):
            target = self._path(source.stem)
            if os.path.lexists(target):
                continue
            temporary: str | None = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="", dir=target.parent,
                    prefix=".skill-", delete=False,
                ) as file:
                    temporary = file.name
                    file.write(source.read_text(encoding="utf-8"))
                    file.flush()
                    os.fsync(file.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
            except (OSError, UnicodeError) as exc:
                raise SkillError(f"cannot initialize skill example {source.stem}: {exc}") from exc
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)

        # 只在首次使用补齐。用户后来删除某个示例时，不再反复生成它。
        try:
            with marker.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            pass
        except OSError as exc:
            raise SkillError(f"cannot record skill initialization: {exc}") from exc

    def _read(self, name: str) -> tuple[SkillEntry, SkillSnapshot]:
        path = self._path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillError(f"cannot read skill {name}: {exc}") from exc
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillError(f"skill {name} requires YAML name and description frontmatter")
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
            metadata = yaml.safe_load("\n".join(lines[1:end]))
            if isinstance(metadata, dict) and {"allowed_tools", "allowed-tools"} & metadata.keys():
                raise SkillError(
                    "Skill migration: remove allowed_tools; skills no longer change permissions"
                )
            entry = SkillEntry.model_validate(metadata)
        except (StopIteration, yaml.YAMLError, ValidationError) as exc:
            raise SkillError(f"invalid skill frontmatter for {name}: {exc}") from exc
        if not entry.description.strip():
            raise SkillError(f"empty skill description: {name}")
        if entry.name != name:
            raise SkillError(f"skill name must match directory: {name}")
        body = "\n".join(lines[end + 1:]).strip()
        if "$ARGUMENTS" in body:
            raise SkillError("Skill migration: remove $ARGUMENTS; use the current user request")
        if not body:
            raise SkillError(f"empty skill body: {name}")
        if estimate_bytes(body) > BODY_TOKENS:
            raise SkillError(
                f"skill {name} exceeds {BODY_TOKENS} estimated tokens; split references"
            )
        return entry, SkillSnapshot(
            name=name, body=body, directory=str(path.parent),
            digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )

    def list_all_skills(self) -> list[SkillEntry]:
        self.ensure_examples()
        try:
            names = sorted(path.name for path in self.root.iterdir()
                           if (path / "SKILL.md").exists())
        except OSError as exc:
            raise SkillError(f"cannot list global skills: {exc}") from exc
        entries = [self._read(name)[0] for name in names]
        if estimate_bytes(render_catalog(entries)) > CATALOG_TOKENS:
            raise SkillError(
                f"skill catalog exceeds {CATALOG_TOKENS} estimated tokens; shorten descriptions"
            )
        return entries

    def load(self, name: str) -> SkillSnapshot:
        self.ensure_examples()
        return self._read(name)[1]


def render_catalog(entries: list[SkillEntry]) -> str:
    return "\n".join(f"- {entry.name}: {entry.description}" for entry in entries)
