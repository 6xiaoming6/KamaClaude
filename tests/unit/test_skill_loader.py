from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.skills.loader import BODY_TOKENS, CATALOG_TOKENS, SkillLoader, render_catalog
from kama_claude.core.skills.model import SkillError


def write_skill(root: Path, name: str = "custom", body: str = "CUSTOM_WORKFLOW", **kwargs) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    description = kwargs.get("description", "Use this workflow for a custom task")
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8")
    return path


def test_examples_only_seed_global_directory_and_never_overwrite(tmp_path: Path) -> None:
    loader = SkillLoader()
    custom = write_skill(loader.root, "review", "MY_REVIEW")
    assert loader.root == (Path.home() / ".kama" / "skills").resolve()
    loader.ensure_examples()
    assert loader.load("review").body == "MY_REVIEW"
    assert {entry.name for entry in loader.list_all_skills()} == {"review", "init", "summarize", "orchestrate"}
    before = custom.read_bytes()
    loader.ensure_examples()
    assert custom.read_bytes() == before
    assert not list(loader.root.glob("*/.skill-*"))


def test_concurrent_bootstrap_is_complete(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: SkillLoader(root).ensure_examples(), range(8)))
    assert len(SkillLoader(root).list_all_skills()) == 4
    assert not list(root.glob("*/.skill-*"))


def test_project_and_flat_files_are_not_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loader = SkillLoader()
    loader.ensure_examples()
    (loader.root / "flat.md").write_text("not loaded", encoding="utf-8")
    write_skill(tmp_path / ".kama" / "skills", "review", "PROJECT_OVERRIDE")
    monkeypatch.chdir(tmp_path)
    assert loader.load("review").body != "PROJECT_OVERRIDE"
    assert "flat" not in {entry.name for entry in loader.list_all_skills()}


@pytest.mark.parametrize("name", ["", "../review", "/review", "a/b", "a\\b", "Review", "--bad", "a" * 65])
def test_invalid_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(SkillError, match="invalid skill name"):
        SkillLoader(tmp_path).load(name)


def test_missing_corrupt_and_migration_errors(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path)
    with pytest.raises(SkillError, match="cannot read"):
        loader.load("missing")
    path = write_skill(tmp_path)
    for content, error in [
        ("plain body", "frontmatter"),
        ("---\nname: custom\ndescription: [\n---\nbody", "frontmatter"),
        ("---\nname: other\ndescription: d\n---\nbody", "match directory"),
        ("---\nname: custom\ndescription: d\nallowed_tools: []\n---\nbody", "migration"),
        ("---\nname: custom\ndescription: d\n---\n$ARGUMENTS", "migration"),
        ("---\nname: custom\ndescription: d\n---\n  ", "empty skill"),
        ("---\nname: custom\ndescription: !!python/object/apply:os.system ['echo forbidden']\n---\nbody", "frontmatter"),
    ]:
        path.write_text(content, encoding="utf-8")
        with pytest.raises(SkillError, match=error):
            loader.load("custom")
    path.write_bytes(b"\xff")
    with pytest.raises(SkillError, match="cannot read"):
        loader.load("custom")


def test_multiline_yaml_and_budgets(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path)
    path = write_skill(tmp_path)
    path.write_text("---\nname: custom\ndescription: >\n  Read and inspect\n  Python code\n---\nWORKFLOW", encoding="utf-8")
    entries = loader.list_all_skills()
    assert next(entry for entry in entries if entry.name == "custom").description == "Read and inspect Python code"
    assert loader.load("custom").body == "WORKFLOW"
    write_skill(tmp_path, body="x" * ((BODY_TOKENS + 1) * 4))
    with pytest.raises(SkillError, match="split references"):
        loader.load("custom")
    write_skill(tmp_path, description="x" * (CATALOG_TOKENS * 4))
    with pytest.raises(SkillError, match="catalog exceeds"):
        loader.list_all_skills()
    assert estimate_bytes(render_catalog(entries)) < CATALOG_TOKENS


def test_directory_link_cannot_escape_root(tmp_path: Path) -> None:
    from tests.unit.test_workspace import directory_link as link_directory
    from tests.unit.test_workspace import unlink_directory

    root, outside = tmp_path / "skills", tmp_path / "outside"
    root.mkdir()
    write_skill(outside, "custom")
    link = root / "custom"
    link_directory(link, outside / "custom")
    try:
        with pytest.raises(SkillError, match="escapes"):
            SkillLoader(root).load("custom")
        # 示例名称的链接同样不能被初始化写入。
        examples = tmp_path / "examples"
        examples.mkdir()
        example_link = examples / "review"
        link_directory(example_link, outside / "custom")
        try:
            with pytest.raises(SkillError, match="escapes"):
                SkillLoader(examples).ensure_examples()
        finally:
            unlink_directory(example_link)
    finally:
        unlink_directory(link)
    assert not os.path.exists(outside / "custom" / ".skill-tmp")


def test_deleted_example_is_not_regenerated_after_first_initialization(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path / "skills")
    loader.ensure_examples()
    (loader.root / "review" / "SKILL.md").unlink()
    loader.ensure_examples()
    assert "review" not in {entry.name for entry in loader.list_all_skills()}
    with pytest.raises(SkillError, match="cannot read"):
        loader.load("review")
