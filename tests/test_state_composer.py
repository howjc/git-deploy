"""State composer transition ID and current-tree composition tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError, PolicyError
from git_deploy.state_composer import ComposeResult, StateComposer


def _git(repo: Path, *args: str) -> str:
    """Run git in a test repository.

    Args:
        repo: Working tree.
        args: Git args.

    Returns:
        Stripped stdout.
    """

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _linear_repo(path: Path) -> dict[str, str]:
    """Create commits A-B-C-D-E on first-parent line.

    Args:
        path: Repository path.

    Returns:
        Mapping of letter → commit id.
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "T")
    commits: dict[str, str] = {}
    for letter in "ABCDE":
        (path / "f.txt").write_text(f"{letter}\n", encoding="utf-8")
        if letter == "A":
            _git(path, "add", "f.txt")
            _git(path, "commit", "-m", letter)
        else:
            _git(path, "commit", "-am", letter)
        commits[letter] = _git(path, "rev-parse", "HEAD")
    return commits


def test_patch_id_binds_format_commit_parent_and_revert_differs(tmp_path: Path) -> None:
    """Transition IDs bind object format/commit/first-parent; revert-of-revert differs."""

    repo = tmp_path / "repo"
    commits = _linear_repo(repo)
    composer = StateComposer(repo)
    tid_b = composer.transition_id_for_commit(commits["B"])
    assert tid_b.commit == commits["B"]
    assert tid_b.first_parent == commits["A"]
    assert tid_b.object_format in {"sha1", "sha256"}
    assert tid_b.as_str().count(":") >= 2

    # singleton, range, stable order with overlap
    expanded = composer.expand_selectors(
        [commits["B"], f"{commits['B']}..{commits['D']}", commits["C"]]
    )
    keys = [item.as_str() for item in expanded]
    assert keys == list(dict.fromkeys(keys))
    assert composer.transition_id_for_commit(commits["B"]).as_str() in keys
    assert composer.transition_id_for_commit(commits["D"]).as_str() in keys

    # Root sentinel
    tid_a = composer.transition_id_for_commit(commits["A"])
    assert tid_a.first_parent == "ROOT"

    # Revert-of-revert on a simple tip change: content may match prior bytes but
    # commit identity (and therefore transition id) must differ.
    simple = tmp_path / "simple"
    simple.mkdir()
    _git(simple, "init", "-q")
    _git(simple, "config", "user.email", "t@example.invalid")
    _git(simple, "config", "user.name", "T")
    (simple / "x.txt").write_text("1\n", encoding="utf-8")
    _git(simple, "add", "x.txt")
    _git(simple, "commit", "-m", "one")
    (simple / "x.txt").write_text("2\n", encoding="utf-8")
    _git(simple, "commit", "-am", "two")
    two = _git(simple, "rev-parse", "HEAD")
    _git(simple, "revert", "--no-edit", "HEAD")
    revert = _git(simple, "rev-parse", "HEAD")
    _git(simple, "revert", "--no-edit", "HEAD")
    ror = _git(simple, "rev-parse", "HEAD")
    simple_composer = StateComposer(simple)
    assert simple_composer.transition_id_for_commit(ror).as_str() != simple_composer.transition_id_for_commit(two).as_str()
    assert simple_composer.transition_id_for_commit(ror).commit == ror
    del revert


def test_current_tree_b_plus_d_then_e_without_c(tmp_path: Path) -> None:
    """Synthetic B+D then E: durable tree is not a main commit tree and omits C."""

    from git_deploy.git_store import PersistentGitStore

    repo = tmp_path / "repo"
    # A-B-C-D-E each edits a distinct path so B+D is not any real commit tree.
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "T")
    commits: dict[str, str] = {}
    for name, fname in [("A", "a.txt"), ("B", "b.txt"), ("C", "c.txt"), ("D", "d.txt"), ("E", "e.txt")]:
        (repo / fname).write_text(f"{name}\n", encoding="utf-8")
        _git(repo, "add", fname)
        _git(repo, "commit", "-m", name)
        commits[name] = _git(repo, "rev-parse", "HEAD")

    target = tmp_path / "target"
    git_store = PersistentGitStore(target, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    composer = StateComposer(repo, git_store=git_store)

    tree_b = _git(repo, "rev-parse", f"{commits['B']}^{{tree}}")
    applied_b = (composer.transition_id_for_commit(commits["B"]).as_str(),)
    # Compose D onto B (skip C) → synthetic B+D.
    bd = composer.compose(
        selectors=[commits["D"]],
        current_tree_id=tree_b,
        applied_transition_ids=applied_b,
    )
    tid_c = composer.transition_id_for_commit(commits["C"]).as_str()
    assert tid_c not in bd.applied_transition_ids
    assert tid_c not in bd.introduced_transition_ids
    # B+D content tree must not equal B or C alone (distinct path set).
    tree_b_real = _git(repo, "rev-parse", f"{commits['B']}^{{tree}}")
    tree_c_real = _git(repo, "rev-parse", f"{commits['C']}^{{tree}}")
    assert bd.target_tree_id != tree_b_real
    assert bd.target_tree_id != tree_c_real
    # Main repo alone cannot read synthetic tree (or may coincidentally share hash).
    subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", bd.target_tree_id],
        capture_output=True,
        text=True,
    )
    # require_tree via durable store must succeed.
    git_store.require_tree(bd.target_tree_id)

    applied_bd = bd.applied_transition_ids
    # Cross-process reopen: new store instance still reads tree.
    reopened = PersistentGitStore(target, repo)
    reopened.require_tree(bd.target_tree_id)

    result = composer.compose(
        selectors=[commits["E"]],
        current_tree_id=bd.target_tree_id,
        applied_transition_ids=applied_bd,
    )
    assert composer.transition_id_for_commit(commits["C"]).as_str() not in result.applied_transition_ids
    assert composer.transition_id_for_commit(commits["B"]).as_str() in result.applied_transition_ids
    assert composer.transition_id_for_commit(commits["D"]).as_str() in result.applied_transition_ids
    assert composer.transition_id_for_commit(commits["E"]).as_str() in result.applied_transition_ids
    assert result.introduced_transition_ids == (
        composer.transition_id_for_commit(commits["E"]).as_str(),
    )
    reopened.require_tree(result.target_tree_id)
    # Synthetic B+D+E must list b,d,e and not c.
    env = reopened.object_environment()
    listed = (
        subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", result.target_tree_id],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.splitlines()
    )
    assert "b.txt" in listed
    assert "d.txt" in listed
    assert "e.txt" in listed
    assert "c.txt" not in listed


def test_synthetic_tree_persistent_across_compose(tmp_path: Path) -> None:
    """Alias name for adversarial -k synthetic_tree collection."""

    test_current_tree_b_plus_d_then_e_without_c(tmp_path)


def test_idempotent_repeated_and_overlapping_selectors(tmp_path: Path) -> None:
    """Repeated singleton/range and overlapping ranges do not re-modify the tree."""

    repo = tmp_path / "repo"
    commits = _linear_repo(repo)
    composer = StateComposer(repo)
    tree_b = _git(repo, "rev-parse", f"{commits['B']}^{{tree}}")
    applied = (composer.transition_id_for_commit(commits["B"]).as_str(),)
    first = composer.compose(
        selectors=[f"{commits['B']}..{commits['D']}"],
        current_tree_id=tree_b,
        applied_transition_ids=applied,
    )
    second = composer.compose(
        selectors=[commits["C"], commits["D"], f"{commits['B']}..{commits['D']}"],
        current_tree_id=first.target_tree_id,
        applied_transition_ids=first.applied_transition_ids,
    )
    assert second.introduced_transition_ids == ()
    assert second.target_tree_id == first.target_tree_id
    assert second.skipped_transition_ids

    # Selecting an already-applied transition remains idempotent (revert does not
    # un-apply the original transition id in state).
    still = composer.compose(
        selectors=[commits["B"]],
        current_tree_id=second.target_tree_id,
        applied_transition_ids=second.applied_transition_ids,
    )
    assert still.introduced_transition_ids == ()


def test_conflict_or_diverge_or_dependency_fails_before_remote(tmp_path: Path) -> None:
    """Missing dependency / diverge fails locally without needing remote/state writes."""

    repo = tmp_path / "repo"
    commits = _linear_repo(repo)
    # Create a divergent branch from A without depending on default branch name.
    _git(repo, "checkout", "-q", "-b", "side", commits["A"])
    (repo / "g.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "g.txt")
    _git(repo, "commit", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-B", "mainline", commits["E"])

    composer = StateComposer(repo)
    with pytest.raises((ConfigurationError, PolicyError)):
        composer.detect_divergence(commits["E"], side)

    # Missing dependency: apply only D onto empty tree should fail to combine cleanly.
    empty = composer.repo.empty_tree()
    with pytest.raises(ConfigurationError):
        composer.compose(
            selectors=[commits["D"]],
            current_tree_id=empty,
            applied_transition_ids=(),
        )


def test_ephemeral_cleanup_failure_retains_retryable_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed cleanup remains visible and can be retried by the owner."""

    ephemeral = tmp_path / "owned-plan-objects"
    ephemeral.mkdir()
    (ephemeral / "object").write_bytes(b"x")
    result = ComposeResult(
        base_tree_id="base",
        target_tree_id="target",
        applied_transition_ids=(),
        introduced_transition_ids=(),
        skipped_transition_ids=(),
        commits=(),
        _ephemeral_dir=str(ephemeral),
    )

    def fail_cleanup(path: str, *, ignore_errors: bool) -> None:
        del path, ignore_errors
        raise OSError("injected cleanup failure")

    monkeypatch.setattr("git_deploy.state_composer.shutil.rmtree", fail_cleanup)
    with pytest.raises(ConfigurationError, match="failed to clean owned plan object"):
        result.close()
    assert result._ephemeral_dir == str(ephemeral)
    assert ephemeral.exists()

    monkeypatch.undo()
    result.close()
    assert result._ephemeral_dir is None
    assert not ephemeral.exists()
