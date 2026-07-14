"""Git object planning tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError, PolicyError
from git_deploy.gitrepo import GitDeploymentPlanner
from git_deploy.models import ProjectConfig


def _git(repository: Path, *args: str) -> str:
    """Run Git in a test repository and return stripped stdout.

    Args:
        repository: Temporary Git working tree.
        args: Git arguments.

    Returns:
        Stripped standard output.
    """

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> str:
    """Commit all tracked test changes and return the commit ID.

    Args:
        repository: Temporary Git working tree.
        message: Commit message.

    Returns:
        Full commit identifier.
    """

    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    """Create an initialized test repository with local author settings.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Initialized repository path.
    """

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    return repository


def test_plan_covers_add_modify_delete_and_rename_from_commit_objects(tmp_path: Path) -> None:
    """Plan common status types and ignore dirty working-tree bytes."""

    repository = _repository(tmp_path)
    (repository / "keep.txt").write_text("old\n", encoding="utf-8")
    (repository / "remove.txt").write_text("remove\n", encoding="utf-8")
    (repository / "old-name.txt").write_text("rename\n", encoding="utf-8")
    older = _commit(repository, "old")

    (repository / "keep.txt").write_text("committed\n", encoding="utf-8")
    (repository / "remove.txt").unlink()
    _git(repository, "mv", "old-name.txt", "new-name.txt")
    (repository / "added.txt").write_text("added\n", encoding="utf-8")
    newer = _commit(repository, "new")
    (repository / "keep.txt").write_text("dirty worktree\n", encoding="utf-8")

    project = ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    planner = GitDeploymentPlanner(project)
    plan = planner.build(older, newer)
    operations = {(item.action, item.path) for item in plan.files}

    assert ("upload", "keep.txt") in operations
    assert ("upload", "added.txt") in operations
    assert ("delete", "remove.txt") in operations
    assert ("upload", "new-name.txt") in operations
    assert ("delete", "old-name.txt") in operations
    keep = next(item for item in plan.files if item.path == "keep.txt")
    assert planner.target_bytes(plan, keep) == b"committed\n"


def test_protected_environment_file_change_is_rejected(tmp_path: Path) -> None:
    """Reject built-in protected files even when an include glob selects them."""

    repository = _repository(tmp_path)
    (repository / ".env").write_text("OLD=1\n", encoding="utf-8")
    older = _commit(repository, "old")
    (repository / ".env").write_text("NEW=1\n", encoding="utf-8")
    newer = _commit(repository, "new")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    with pytest.raises(PolicyError, match="protected path"):
        planner.build(older, newer)


def test_symlink_change_is_rejected(tmp_path: Path) -> None:
    """Reject target symlinks because transports cannot preserve their semantics safely."""

    repository = _repository(tmp_path)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    older = _commit(repository, "old")
    (repository / "link.txt").symlink_to("base.txt")
    newer = _commit(repository, "new")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    with pytest.raises(PolicyError, match="symlink"):
        planner.build(older, newer)


def test_file_created_and_deleted_inside_range_produces_no_operation(tmp_path: Path) -> None:
    """Ignore a transient path absent from both range endpoint commits."""

    repository = _repository(tmp_path)
    _git(repository, "commit", "--allow-empty", "-m", "base")
    older = _git(repository, "rev-parse", "HEAD")
    (repository / "transient.txt").write_text("temporary\n", encoding="utf-8")
    _commit(repository, "add transient")
    (repository / "transient.txt").unlink()
    newer = _commit(repository, "remove transient")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    assert planner.build(older, newer).files == ()


def test_single_revision_selects_only_that_commit(tmp_path: Path) -> None:
    """Treat one revision as its first-parent-to-commit change."""

    repository = _repository(tmp_path)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repository, "base")
    (repository / "single.txt").write_text("single\n", encoding="utf-8")
    selected = _commit(repository, "single")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    plan = planner.build_revisions([selected])

    assert plan.from_commit == base
    assert plan.to_commit == selected
    assert plan.revision_specs == (selected,)
    assert [(operation.action, operation.path) for operation in plan.files] == [
        ("upload", "single.txt")
    ]


def test_continuous_revision_range_uses_real_target_commit(tmp_path: Path) -> None:
    """Keep a continuous range equivalent to the former source/target plan."""

    repository = _repository(tmp_path)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repository, "base")
    (repository / "one.txt").write_text("one\n", encoding="utf-8")
    _commit(repository, "one")
    (repository / "two.txt").write_text("two\n", encoding="utf-8")
    target = _commit(repository, "two")
    selector = f"{base}..{target}"
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    plan = planner.build_revisions([selector])

    assert plan.from_commit == base
    assert plan.to_commit == target
    assert {operation.path for operation in plan.files} == {"one.txt", "two.txt"}


def test_head_range_is_frozen_to_commit_hashes_for_history(tmp_path: Path) -> None:
    """Persist the commits selected by HEAD instead of its movable spelling."""

    repository = _repository(tmp_path)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _commit(repository, "base")
    (repository / "one.txt").write_text("one\n", encoding="utf-8")
    older = _commit(repository, "one")
    (repository / "two.txt").write_text("two\n", encoding="utf-8")
    newer = _commit(repository, "two")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    plan = planner.build_revisions(["HEAD^..HEAD"])

    assert plan.revision_specs == (f"{older}..{newer}",)
    assert plan.to_commit == newer


def test_non_contiguous_revisions_exclude_omitted_commit_files(tmp_path: Path) -> None:
    """Compose selected patches without leaking files from skipped commits."""

    repository = _repository(tmp_path)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repository, "base")
    (repository / "one.txt").write_text("one\n", encoding="utf-8")
    first = _commit(repository, "one")
    (repository / "skipped.txt").write_text("skipped\n", encoding="utf-8")
    _commit(repository, "skipped")
    (repository / "three.txt").write_text("three\n", encoding="utf-8")
    third = _commit(repository, "three")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )
    object_directory = repository / ".git" / "objects"
    objects_before = {
        path.relative_to(object_directory)
        for path in object_directory.rglob("*")
        if path.is_file()
    }

    plan = planner.build_revisions([third, first])
    objects_after = {
        path.relative_to(object_directory)
        for path in object_directory.rglob("*")
        if path.is_file()
    }

    assert plan.from_commit == base
    assert plan.to_commit not in {first, third}
    assert plan.revision_specs == (third, first)
    assert {operation.path for operation in plan.files} == {"one.txt", "three.txt"}
    assert objects_after == objects_before
    three = next(operation for operation in plan.files if operation.path == "three.txt")
    assert planner.target_bytes(plan, three) == b"three\n"


def test_overlapping_revision_selectors_are_deduplicated(tmp_path: Path) -> None:
    """Apply a commit once when singleton and range selectors overlap."""

    repository = _repository(tmp_path)
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repository, "base")
    (repository / "one.txt").write_text("one\n", encoding="utf-8")
    first = _commit(repository, "one")
    (repository / "two.txt").write_text("two\n", encoding="utf-8")
    second = _commit(repository, "two")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    plan = planner.build_revisions([f"{base}..{second}", first, second])

    assert plan.from_commit == base
    assert plan.to_commit == second
    assert {operation.path for operation in plan.files} == {"one.txt", "two.txt"}


def test_non_contiguous_revisions_do_not_leak_omitted_same_file_changes(
    tmp_path: Path,
) -> None:
    """Replay a later hunk without taking an earlier skipped hunk in that file."""

    repository = _repository(tmp_path)
    original_lines = [f"line-{number}\n" for number in range(1, 11)]
    (repository / "value.txt").write_text("".join(original_lines), encoding="utf-8")
    _commit(repository, "base")
    (repository / "selected-one.txt").write_text("one\n", encoding="utf-8")
    first = _commit(repository, "first")
    skipped_lines = list(original_lines)
    skipped_lines[1] = "skipped-line-2\n"
    (repository / "value.txt").write_text("".join(skipped_lines), encoding="utf-8")
    _commit(repository, "skipped")
    target_lines = list(skipped_lines)
    target_lines[8] = "selected-line-9\n"
    (repository / "value.txt").write_text("".join(target_lines), encoding="utf-8")
    third = _commit(repository, "third")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    plan = planner.build_revisions([first, third])

    value = next(operation for operation in plan.files if operation.path == "value.txt")
    composed_lines = list(original_lines)
    composed_lines[8] = "selected-line-9\n"
    assert planner.target_bytes(plan, value) == "".join(composed_lines).encode()


def test_non_contiguous_revision_dependency_conflict_is_rejected(tmp_path: Path) -> None:
    """Stop locally when a selected patch requires an omitted same-line change."""

    repository = _repository(tmp_path)
    (repository / "value.txt").write_text("base\n", encoding="utf-8")
    _commit(repository, "base")
    (repository / "value.txt").write_text("first\n", encoding="utf-8")
    first = _commit(repository, "first")
    (repository / "value.txt").write_text("skipped\n", encoding="utf-8")
    _commit(repository, "skipped")
    (repository / "value.txt").write_text("third\n", encoding="utf-8")
    third = _commit(repository, "third")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )

    with pytest.raises(ConfigurationError, match="cannot be combined cleanly"):
        planner.build_revisions([first, third])


def test_root_commit_can_be_selected_without_writing_git_objects(tmp_path: Path) -> None:
    """Use a temporary empty-tree object when the selected commit is the root."""

    repository = _repository(tmp_path)
    (repository / "root.txt").write_text("root\n", encoding="utf-8")
    root = _commit(repository, "root")
    planner = GitDeploymentPlanner(
        ProjectConfig(name="demo", repository=repository, remote_root="/srv/demo")
    )
    object_directory = repository / ".git" / "objects"
    objects_before = {
        path.relative_to(object_directory)
        for path in object_directory.rglob("*")
        if path.is_file()
    }

    plan = planner.build_revisions([root])
    objects_after = {
        path.relative_to(object_directory)
        for path in object_directory.rglob("*")
        if path.is_file()
    }

    assert plan.from_commit == planner.repository.empty_tree()
    assert [(operation.action, operation.path) for operation in plan.files] == [
        ("upload", "root.txt")
    ]
    assert objects_after == objects_before
