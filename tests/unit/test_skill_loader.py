from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kama_claude.core.compact.estimator import estimate_bytes
from kama_claude.core.skills.context import render_catalog
from kama_claude.core.skills.loader import BODY_TOKENS, CATALOG_TOKENS, SkillLoader
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
    assert {entry.name for entry in loader.list_all_skills()} == {"review", "init", "summarize", "orchestrate", "github-issue-fix"}
    before = custom.read_bytes()
    loader.ensure_examples()
    assert custom.read_bytes() == before
    assert not list(loader.root.glob("*/.skill-*"))


def test_concurrent_bootstrap_is_complete(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: SkillLoader(root).ensure_examples(), range(8)))
    assert len(SkillLoader(root).list_all_skills()) == 5
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


# 功能：验证旧空标记用户能获得新增 Skill，删改过的旧示例保持原样。
# 设计：模拟升级前已初始化的真实目录，而非只测试首次安装的空目录。
def test_legacy_initialization_adds_issue_skill_without_restoring_old_examples(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    review = write_skill(root, "review", "USER_REVIEW")
    (root / ".examples-initialized").touch()
    loader = SkillLoader(root)
    snapshot = loader.load("github-issue-fix")
    assert snapshot.name == "github-issue-fix"
    assert estimate_bytes(snapshot.body) <= BODY_TOKENS
    assert review.read_text(encoding="utf-8").endswith("USER_REVIEW\n")
    assert not (root / "summarize").exists()
    marker = (root / ".examples-initialized").read_text(encoding="utf-8")
    assert "github-issue-fix" in marker.splitlines()
    (root / "github-issue-fix" / "SKILL.md").unlink()
    assert "github-issue-fix" not in {entry.name for entry in loader.list_all_skills()}


# 功能：验证新增内置 Skill 与用户同名文件共存时不覆盖用户正文。
# 设计：覆盖升级补齐的名称冲突，随后删除也不能被下一次加载恢复。
def test_upgrade_keeps_custom_issue_skill_and_honors_later_deletion(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    custom = write_skill(root, "github-issue-fix", "MY_ISSUE_WORKFLOW")
    (root / ".examples-initialized").touch()
    loader = SkillLoader(root)
    assert loader.load("github-issue-fix").body == "MY_ISSUE_WORKFLOW"
    custom.unlink()
    with pytest.raises(SkillError, match="cannot read"):
        loader.load("github-issue-fix")


# 功能：验证补齐标记提交失败会报错，重试不会覆盖已安装或用户编辑的 Skill。
# 设计：只注入原子替换故障，检查无半份标记和临时残留，并真实重试读取。
def test_initialization_record_failure_is_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    loader = SkillLoader(root)
    with monkeypatch.context() as patch:
        patch.setattr("kama_claude.core.skills.loader.os.replace",
                      lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(SkillError, match="cannot record"):
            loader.ensure_examples()
    assert not (root / ".examples-initialized").exists()
    assert not list(root.glob(".examples-*"))
    write_skill(root, "github-issue-fix", "EDITED_AFTER_FAILURE")
    assert loader.load("github-issue-fix").body == "EDITED_AFTER_FAILURE"
    assert len(loader.list_all_skills()) == 5


# 功能：验证 Windows 标记文件短暂共享冲突可有界重试，内容仍完整且不覆盖 Skill。
# 设计：在原子替换处仅注入一次 PermissionError，再真实完成文件替换。
def test_initialization_record_retries_transient_sharing_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replace = os.replace
    attempts = []

    # 首次模拟并发读取占用，之后使用真实系统调用。
    def busy_once(source, destination):
        attempts.append(destination)
        if len(attempts) == 1:
            raise PermissionError("sharing violation")
        replace(source, destination)

    monkeypatch.setattr("kama_claude.core.skills.loader.os.replace", busy_once)
    loader = SkillLoader(tmp_path / "skills")
    assert len(loader.list_all_skills()) == 5
    assert len(attempts) == 2
    assert {p.name for p in loader.root.glob(".examples-*")} == {".examples-initialized"}
