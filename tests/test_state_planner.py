"""Source diff planner and static no-op tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from git_deploy.state_composer import StateComposer
from git_deploy.state_planner import StatePlanner


def _git(repo: Path, *args: str) -> str:
    """Run git.

    Args:
        repo: Repo path.
        args: Args.

    Returns:
        Stdout.
    """

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _two_file_repo(path: Path) -> tuple[str, str, str]:
    """Create a repo with add/modify/delete across two commits.

    Args:
        path: Repo path.

    Returns:
        older, newer commits and middle if needed.
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "T")
    (path / "keep.txt").write_text("k1\n", encoding="utf-8")
    (path / "gone.txt").write_text("g\n", encoding="utf-8")
    (path / "skip.log").write_text("s\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "one")
    older = _git(path, "rev-parse", "HEAD")
    (path / "keep.txt").write_text("k2\n", encoding="utf-8")
    (path / "new.txt").write_text("n\n", encoding="utf-8")
    (path / "gone.txt").unlink()
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "two")
    newer = _git(path, "rev-parse", "HEAD")
    return older, newer, _git(path, "rev-parse", f"{older}^{{tree}}")


def test_source_diff_uses_current_tree_as_before(tmp_path: Path) -> None:
    """Planner diffs use current tree as before for add/modify/delete and filters."""

    from git_deploy.git_store import PersistentGitStore

    repo = tmp_path / "repo"
    older, newer, tree_old = _two_file_repo(repo)
    target = tmp_path / "target"
    git_store = PersistentGitStore(target, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    planner = StatePlanner(
        repo,
        include=("**",),
        exclude=("*.log",),
        protected=(),
        remote_root="/srv",
        git_store=git_store,
    )
    composer = StateComposer(repo, git_store=git_store)
    applied = (composer.transition_id_for_commit(older).as_str(),)
    plan = planner.plan_selectors(
        [newer],
        current_tree_id=tree_old,
        applied_transition_ids=applied,
    )
    paths = {item.path: item for item in plan.files}
    assert "keep.txt" in paths
    assert paths["keep.txt"].expected_before_sha256 is not None
    assert paths["keep.txt"].target_sha256 is not None
    assert "new.txt" in paths
    assert paths["new.txt"].expected_before_sha256 is None
    assert "gone.txt" in paths
    assert paths["gone.txt"].action == "delete"
    assert plan.before_tree_id == tree_old


def test_static_noop_marks_remote_unverified(tmp_path: Path) -> None:
    """Repeated selectors yield static no-op plan without requiring remote."""

    repo = tmp_path / "repo"
    older, newer, _tree = _two_file_repo(repo)
    planner = StatePlanner(repo, remote_root="/srv")
    composer = StateComposer(repo)
    tree_new = _git(repo, "rev-parse", f"{newer}^{{tree}}")
    applied = (
        composer.transition_id_for_commit(older).as_str(),
        composer.transition_id_for_commit(newer).as_str(),
    )
    plan = planner.plan_selectors(
        [newer, f"{older}..{newer}"],
        current_tree_id=tree_new,
        applied_transition_ids=applied,
        static_only=True,
    )
    assert plan.static_noop or not plan.files
    assert plan.remote_unverified is True
    assert plan.introduced_transition_ids == ()
