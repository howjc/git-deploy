"""Git object planning tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.errors import PolicyError
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
