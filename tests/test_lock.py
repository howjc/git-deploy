"""Git common-dir state and target-lock behavior across linked worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from git_deploy.errors import PlanError
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore, TargetState


def test_target_lock_reports_owner_and_releases(tmp_path: Path) -> None:
    """A second process scope fails fast with owner metadata, then can acquire."""

    state_root = tmp_path / "git-deploy"
    first = TargetLock(state_root, "prod")
    second = TargetLock(state_root, "prod")
    first.acquire()
    try:
        with pytest.raises(PlanError, match='"pid"'):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
    second.release()


def test_linked_worktrees_share_state_and_lock(git_project: Path, tmp_path: Path) -> None:
    """Linked worktrees resolve one common state and mutual-exclusion directory."""

    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)],
        cwd=git_project,
        check=True,
    )
    primary = GitRepository(git_project)
    secondary = GitRepository(linked)

    assert primary.git_dir() != secondary.git_dir()
    assert primary.common_dir() == secondary.common_dir()
    first_store = StateStore(primary.common_dir())
    second_store = StateStore(secondary.common_dir())
    state = TargetState(1, "prod", "sftp:x", primary.head(), 1, {})
    first_store.save(state)
    assert second_store.load("prod") == state

    with TargetLock(first_store.base, "prod"):
        with pytest.raises(PlanError, match="already being deployed"):
            TargetLock(second_store.base, "prod").acquire()


def test_legacy_per_worktree_state_migrates_without_deletion(
    git_project: Path,
    tmp_path: Path,
) -> None:
    """First v1.1 deploy copies a valid v1.0 state into common-dir storage safely."""

    legacy = StateStore(tmp_path / "worktree-git-dir")
    common = StateStore(GitRepository(git_project).common_dir())
    state = TargetState(1, "prod", "sftp:x", GitRepository(git_project).head(), 1, {})
    legacy.save(state)

    assert common.migrate_from(legacy, "prod")
    assert common.load("prod") == state
    assert legacy.load("prod") == state
    assert not common.migrate_from(legacy, "prod")


def test_partial_acquire_fsync_failure_releases_for_immediate_retry(
    tmp_path: Path,
) -> None:
    """Metadata/fsync failure after flock must unlock so the next acquire works."""

    state_root = tmp_path / "git-deploy"
    first = TargetLock(state_root, "prod")
    with patch("git_deploy.lock.os.fsync", side_effect=OSError(28, "No space left")):
        with pytest.raises(OSError, match="No space left"):
            first.acquire()
    assert first._handle is None
    second = TargetLock(state_root, "prod")
    second.acquire()
    second.release()


def test_partial_acquire_metadata_write_failure_closes_handle(
    tmp_path: Path,
) -> None:
    """Owner metadata write failure unlocks and closes without leaving _handle."""

    state_root = tmp_path / "git-deploy"
    lock = TargetLock(state_root, "staging")
    with patch("git_deploy.lock.json.dump", side_effect=OSError("disk write failed")):
        with pytest.raises(OSError, match="disk write failed"):
            lock.acquire()
    assert lock._handle is None
    retry = TargetLock(state_root, "staging")
    retry.acquire()
    retry.release()
