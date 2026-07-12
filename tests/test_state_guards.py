"""State identity and transaction preflight guard tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.errors import PolicyError
from git_deploy.expected_state import ExpectedStateStore, build_expected_state
from git_deploy.state_guards import StateGuards
from git_deploy.target_identity import resolve_target_identity
from git_deploy.transaction import TransactionStore


def test_guards_block_mismatch_corruption_and_open_tx(tmp_path: Path) -> None:
    """Target/policy mismatch, state damage, and open transactions all block; force ignored."""

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    root = tmp_path / "t"
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol-a",
    )
    store.cas_advance(expected_generation=None, state=state)

    # Policy mismatch
    guards = StateGuards(root, identity, expected_policy="pol-b")
    report = guards.check(allow_force=True)
    assert not report.ok
    assert any("policy" in reason for reason in report.reasons)

    # Open transaction
    tx = TransactionStore(root)
    tx.create(target_id=identity.target_id, stage="prepared")
    guards2 = StateGuards(root, identity, expected_policy="pol-a")
    with pytest.raises(PolicyError, match="transaction"):
        guards2.require_clear(force=True)

    # Physical mismatch
    other = resolve_target_identity(
        {"protocol": "sftp", "host": "other.example"},
        "demo",
        remote_root="/srv",
    )
    guards3 = StateGuards(root, other, expected_policy="pol-a")
    report3 = guards3.check()
    assert not report3.ok


def test_missing_cas_blocks_guards(tmp_path: Path) -> None:
    """Missing CAS content refs block deploy/plan guards."""

    import hashlib

    from git_deploy.expected_state import FileEntry

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    root = tmp_path / "t"
    store = ExpectedStateStore(root, identity)
    digest = hashlib.sha256(b"payload").hexdigest()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol-a",
        files=(
            FileEntry(path="a.txt", owner="source", content_sha256=digest, exists=True),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    # Do not put CAS object.
    guards = StateGuards(root, identity, expected_policy="pol-a")
    report = guards.check()
    assert not report.ok
    assert any("CAS" in reason for reason in report.reasons)


def test_corrupt_cas_blocks_guards(tmp_path: Path) -> None:
    """Tampered CAS object rehash failure blocks guards."""

    import hashlib

    from git_deploy.expected_state import FileEntry
    from git_deploy.object_store import ContentAddressedStore

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    root = tmp_path / "t"
    store = ExpectedStateStore(root, identity)
    digest = hashlib.sha256(b"payload").hexdigest()
    cas = ContentAddressedStore(root)
    cas.put(b"payload")
    # Corrupt the object bytes in place.
    path = cas.path_for(digest)
    path.write_bytes(b"tampered-not-matching-hash")
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol-a",
        files=(
            FileEntry(path="a.txt", owner="source", content_sha256=digest, exists=True),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    guards = StateGuards(root, identity, expected_policy="pol-a")
    report = guards.check()
    assert not report.ok
    assert any("CAS" in reason for reason in report.reasons)


def test_git_object_alternate_mismatch_blocks(tmp_path: Path) -> None:
    """Persistent git store alternate/repository mismatch fails closed."""

    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from git_deploy.git_store import PersistentGitStore

    root = tmp_path / "target"
    git_store = PersistentGitStore(root, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    # Point repository_path at a different path to force mismatch.
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
    marker = root / "git" / "repository_path"
    from git_deploy.durable_io import durable_publish

    durable_publish(marker, (str(other) + "\n").encode("utf-8"))
    # Also corrupt repository_identity to mismatch alternate.
    identity_path = root / "git" / "repository_identity"
    durable_publish(identity_path, b"/nonexistent/objects\n")

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol-a",
    )
    store.cas_advance(expected_generation=None, state=state)
    guards = StateGuards(root, identity, expected_policy="pol-a")
    report = guards.check()
    assert not report.ok
    assert any("git" in reason.lower() or "identity" in reason.lower() for reason in report.reasons)


def test_missing_git_store_blocks_when_source_tree_set(tmp_path: Path) -> None:
    """Entire missing git/ store fails closed when current has source_tree_id."""

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    root = tmp_path / "t"
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="deadbeef" * 5,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol-a",
    )
    store.cas_advance(expected_generation=None, state=state)
    assert not (root / "git").exists()
    guards = StateGuards(root, identity, expected_policy="pol-a")
    report = guards.check(allow_force=True)
    assert not report.ok
    assert any("git" in r.lower() for r in report.reasons)


def test_force_integrity_cannot_bypass_missing_cas(tmp_path: Path) -> None:
    """--force cannot bypass missing CAS integrity guards."""

    import hashlib

    from git_deploy.expected_state import FileEntry
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    root = tmp_path / "t"
    gs = PersistentGitStore(root, repo)
    gs.ensure_layout()
    gs._publish_repository_identity()
    empty = GitRepository(repo).empty_tree()
    store = ExpectedStateStore(root, identity)
    digest = hashlib.sha256(b"nope").hexdigest()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol-a",
        files=(FileEntry(path="a.txt", owner="source", content_sha256=digest, exists=True),),
    )
    store.cas_advance(expected_generation=None, state=state)
    guards = StateGuards(root, identity, expected_policy="pol-a")
    with pytest.raises(PolicyError, match="CAS|integrity"):
        guards.require_clear(force=True)
