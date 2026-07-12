"""Persistent Git object store tests (S06)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError
from git_deploy.git_store import PersistentGitStore


def _git(repo: Path, *args: str) -> str:
    """Run git in a test repository.

    Args:
        repo: Working tree.
        args: Git arguments.

    Returns:
        Stripped stdout.
    """

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_chain(path: Path) -> tuple[Path, str, str, str]:
    """Create a three-commit repository A-B-C.

    Args:
        path: Repository path.

    Returns:
        Repo path and commits A, B, C.
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "T")
    (path / "f.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-m", "a")
    a = _git(path, "rev-parse", "HEAD")
    (path / "f.txt").write_text("b\n", encoding="utf-8")
    _git(path, "commit", "-am", "b")
    b = _git(path, "rev-parse", "HEAD")
    (path / "f.txt").write_text("c\n", encoding="utf-8")
    _git(path, "commit", "-am", "c")
    c = _git(path, "rev-parse", "HEAD")
    return path, a, b, c


def test_git_store_persists_tree_without_mutating_main(tmp_path: Path) -> None:
    """Composed trees remain readable after reopen; main repo object count stable."""

    repo, a, b, c = _repo_with_chain(tmp_path / "repo")
    store = PersistentGitStore(tmp_path / "target", repo)
    before = store.main_object_count()
    tree = store.persist_tree_from_compose(a, [b, c])
    after = store.main_object_count()
    assert after == before

    reopened = PersistentGitStore(tmp_path / "target", repo)
    reopened.require_tree(tree)


def test_git_store_rejects_repository_identity_mismatch(tmp_path: Path) -> None:
    """Alternate/repository identity mismatch blocks reads."""

    repo, a, b, _c = _repo_with_chain(tmp_path / "repo")
    store = PersistentGitStore(tmp_path / "target", repo)
    tree = store.persist_tree_from_compose(a, [b])
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "T")
    (other / "x.txt").write_text("x\n", encoding="utf-8")
    _git(other, "add", "x.txt")
    _git(other, "commit", "-m", "x")
    mismatched = PersistentGitStore(tmp_path / "target", other)
    with pytest.raises(ConfigurationError, match="repository identity mismatch"):
        mismatched.require_tree(tree)
