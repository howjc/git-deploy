"""Exact isolated build worktree lifecycle tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.git_store import PersistentGitStore
from git_deploy.state_composer import StateComposer
from git_deploy.worktree import WorktreeManager


def _git(repo: Path, *args: str) -> str:
    """Run Git in a fixture repository and return stripped stdout."""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(path: Path) -> tuple[str, str]:
    """Create two commits and return their commit/tree ids."""

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@e")
    _git(path, "config", "user.name", "T")
    (path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "one")
    first = _git(path, "rev-parse", "HEAD")
    (path / "tracked.txt").write_text("two\n", encoding="utf-8")
    (path / "second.txt").write_text("second\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "two")
    second = _git(path, "rev-parse", "HEAD")
    return first, second


def test_real_tree_exact_and_dirty_main_files_excluded(tmp_path: Path) -> None:
    """A real tree is exact and ignores tracked edits plus untracked main files."""

    repo = tmp_path / "repo"
    first, _second = _repository(repo)
    tree = _git(repo, "rev-parse", f"{first}^{{tree}}")
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked-secret.txt").write_text("must-not-enter\n", encoding="utf-8")
    base = tmp_path / "build-inputs"
    manager = WorktreeManager(repo, base)

    with manager.materialize(tree) as worktree:
        assert worktree.tree_id == tree
        assert (worktree.path / "tracked.txt").read_text(encoding="utf-8") == "one\n"
        assert not (worktree.path / "second.txt").exists()
        assert not (worktree.path / "untracked-secret.txt").exists()
        owned = worktree.path
    assert not owned.exists()
    assert list(base.iterdir()) == []


def test_persistent_synthetic_tree_materializes_exact_files(tmp_path: Path) -> None:
    """A composed tree remains materializable from the persistent object store."""

    repo = tmp_path / "repo"
    first, second = _repository(repo)
    tree = _git(repo, "rev-parse", f"{first}^{{tree}}")
    store = PersistentGitStore(tmp_path / "target", repo)
    composer = StateComposer(repo, git_store=store)
    result = composer.compose(
        selectors=[second],
        current_tree_id=tree,
        applied_transition_ids=(),
    )
    store.require_tree(result.target_tree_id)

    manager = WorktreeManager(repo, tmp_path / "build-inputs")
    with manager.materialize(
        result.target_tree_id,
        object_env=store.object_environment(),
    ) as worktree:
        assert (worktree.path / "tracked.txt").read_text(encoding="utf-8") == "two\n"
        assert (worktree.path / "second.txt").read_text(encoding="utf-8") == "second\n"


@pytest.mark.parametrize("raised", [RuntimeError("boom"), KeyboardInterrupt()])
def test_worktree_cleans_after_exception_and_interrupt(
    tmp_path: Path, raised: BaseException
) -> None:
    """Owned worktree and index are removed after errors and Ctrl-C."""

    repo = tmp_path / "repo"
    first, _second = _repository(repo)
    tree = _git(repo, "rev-parse", f"{first}^{{tree}}")
    base = tmp_path / "build-inputs"
    manager = WorktreeManager(repo, base)

    with pytest.raises(type(raised)):
        with manager.materialize(tree):
            raise raised
    assert list(base.iterdir()) == []
