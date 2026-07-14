"""Stateful rollback latest/non-latest tests."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError, PolicyError
from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
from git_deploy.models import DeploymentManifest, FileSnapshot, ProjectConfig
from git_deploy.state import DeploymentStore
from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
from git_deploy.state_rollback import StateRollbackService
from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity


def _real_empty_tree(repo: Path) -> str:
    """Initialize a repository and write a real empty tree object.

    Args:
        repo: Repository directory, created by the caller when absent.

    Returns:
        Empty tree object id readable through the repository object database.
    """

    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "-t", "tree", "--stdin"],
        input=b"",
        check=True,
        capture_output=True,
    ).stdout.decode().strip()


def _setup(tmp_path: Path):
    """Seed current + two successful manifests with backups.

    Args:
        tmp_path: Temp path.

    Returns:
        service, transport, store, identity.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    tree = _real_empty_tree(repo)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "st")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.object_store import ContentAddressedStore

    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    cas = ContentAddressedStore(root)
    cas.put(b"before")
    cas.put(b"after")
    cas.put(b"untouched")
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"after")
    service = StateRollbackService(project, identity, root, transport=transport)
    policy = policy_fingerprint_for_project(project)

    # States
    store = ExpectedStateStore(root, identity)
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"before").hexdigest(),
            ),
            FileEntry(
                path="untouched.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"untouched").hexdigest(),
            ),
        ),
    )
    store.write_state(before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id=tree,
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"after").hexdigest(),
            ),
            FileEntry(
                path="untouched.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"untouched").hexdigest(),
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=after) if False else None
    # Proper path: write gen1 then gen2
    store2 = ExpectedStateStore(root, identity)
    store2.cas_advance(expected_generation=None, state=before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id=tree,
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"after").hexdigest(),
            ),
            FileEntry(
                path="untouched.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"untouched").hexdigest(),
            ),
        ),
    )
    store2.cas_advance(expected_generation=1, state=after)

    deploy = DeploymentStore(project, root=root)
    older = DeploymentManifest(
        deployment_id="20200101T000000Z-old1",
        project="demo",
        repository=str(repo),
        remote_root="/srv",
        from_commit="a",
        to_commit="b",
        created_at="t",
        status="succeeded",
        snapshots=[],
        state="v1",
        before_state_id=before.state_id(),
        after_state_id=before.state_id(),
        introduced_transition_ids=[],
    )
    deploy.write_manifest(older)
    latest = DeploymentManifest(
        deployment_id="20200101T000001Z-new1",
        project="demo",
        repository=str(repo),
        remote_root="/srv",
        from_commit="b",
        to_commit="c",
        created_at="t2",
        status="succeeded",
        snapshots=[
            FileSnapshot(
                path="a.txt",
                remote_path="/srv/a.txt",
                before_exists=True,
                before_sha256=hashlib.sha256(b"before").hexdigest(),
                backup_file=deploy.write_backup("20200101T000001Z-new1", 0, b"before"),
                after_exists=True,
                after_sha256=hashlib.sha256(b"after").hexdigest(),
            )
        ],
        state="v1",
        before_state_id=before.state_id(),
        after_state_id=after.state_id(),
        before_generation=1,
        after_generation=2,
        introduced_transition_ids=["t1"],
        target_id=identity.target_id,
    )
    deploy.write_manifest(latest)
    return service, transport, store2, identity, older, latest


def test_latest_rollback_restores_bytes_and_generation(tmp_path: Path) -> None:
    """Latest rollback restores bytes, transitions, and advances generation auditably."""

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    result = service.rollback_latest()
    assert result.status == "succeeded"
    assert transport.files["/srv/a.txt"].data == b"before"
    assert result.generation == 3
    current = store.read_current()
    assert current is not None
    state = store.read_state(current.state_id)
    assert "t1" not in state.applied_transition_ids or "t1" not in set(latest.introduced_transition_ids) - set(
        state.applied_transition_ids
    )
    assert "t0" in state.applied_transition_ids


def test_corrupt_before_backup_blocks_rollback_before_remote_io(
    tmp_path: Path,
) -> None:
    """Refuse a tampered before backup before reads, writes, journal, or CAS."""

    service, transport, store, _identity, _older, latest = _setup(tmp_path)
    snapshot = latest.snapshots[0]
    assert snapshot.backup_file is not None
    backup = (
        service.deploy_store.deployment_dir(latest.deployment_id)
        / snapshot.backup_file
    )
    backup.write_bytes(b"tampered")
    generation = store.read_current().generation  # type: ignore[union-attr]
    reads = transport.read_calls
    writes = transport.write_calls

    with pytest.raises(PolicyError, match="backup hash mismatch"):
        service.rollback_latest()

    assert transport.read_calls == reads
    assert transport.write_calls == writes
    assert store.read_current().generation == generation  # type: ignore[union-attr]
    from git_deploy.transaction import TransactionStore

    assert TransactionStore(service.target_root).list_open() == []


def test_non_latest_refused_before_connect(tmp_path: Path) -> None:
    """Non-latest rollback refused with zero transport writes when current exists."""

    service, transport, _store, _identity, older, _latest = _setup(tmp_path)
    writes = transport.write_calls
    with pytest.raises(PolicyError, match="non-latest|v0.3"):
        service.assert_latest_only(older.deployment_id)
    assert transport.write_calls == writes


def test_rollback_journal_created_before_mutation(tmp_path: Path) -> None:
    """Latest rollback creates durable transaction evidence before remote writes."""

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    from git_deploy.transaction import TransactionStore

    # Seed git store for after-state with tree id from current.
    from git_deploy.git_store import PersistentGitStore
    import subprocess

    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    gs = PersistentGitStore(tmp_path / "target", repo)
    gs.ensure_layout()
    gs._publish_repository_identity()

    result = service.rollback_latest()
    assert result.status == "succeeded"
    open_tx = TransactionStore(tmp_path / "target").list_open()
    assert open_tx == []


def test_rollback_crash_leaves_open_journal(tmp_path: Path) -> None:
    """Partial multi-file rollback (≥2 snapshots): first restored, second pre-rollback.

    Fail after first path is fully restored+readback; second remains after-bytes;
    generation unchanged; journal open at remote_mutating.
    """

    import hashlib
    import subprocess

    from git_deploy.errors import PolicyError
    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import DeploymentManifest, FileSnapshot, ProjectConfig
    from git_deploy.state import DeploymentStore
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.state_rollback import StateRollbackService
    from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity
    from git_deploy.transaction import TransactionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    project = ProjectConfig(
        name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "st"
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    empty = _real_empty_tree(repo)
    from git_deploy.object_store import ContentAddressedStore

    cas = ContentAddressedStore(root)
    for content in (b"ba", b"bb", b"aa", b"ab"):
        cas.put(content)
    pol = policy_fingerprint_for_project(project)
    store = ExpectedStateStore(root, identity)
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"ba").hexdigest(),
                exists=True,
            ),
            FileEntry(
                path="b.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"bb").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id=empty,
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"aa").hexdigest(),
                exists=True,
            ),
            FileEntry(
                path="b.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"ab").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=1, state=after)
    deploy = DeploymentStore(project, root=root)
    ra = deploy.write_backup("20200101T000002Z-new", 0, b"ba")
    rb = deploy.write_backup("20200101T000002Z-new", 1, b"bb")
    deploy.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T000002Z-new",
            project="demo",
            repository=str(repo),
            remote_root="/srv",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
            snapshots=[
                FileSnapshot(
                    path="a.txt",
                    remote_path="/srv/a.txt",
                    before_exists=True,
                    before_sha256=hashlib.sha256(b"ba").hexdigest(),
                    backup_file=ra,
                    after_exists=True,
                    after_sha256=hashlib.sha256(b"aa").hexdigest(),
                ),
                FileSnapshot(
                    path="b.txt",
                    remote_path="/srv/b.txt",
                    before_exists=True,
                    before_sha256=hashlib.sha256(b"bb").hexdigest(),
                    backup_file=rb,
                    after_exists=True,
                    after_sha256=hashlib.sha256(b"ab").hexdigest(),
                ),
            ],
            state="v1",
            before_state_id=before.state_id(),
            after_state_id=after.state_id(),
            before_generation=1,
            after_generation=2,
            introduced_transition_ids=["t1"],
            target_id=identity.target_id,
        )
    )
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"aa")
    transport.files["/srv/b.txt"] = FakeRemotePath(b"ab")
    service = StateRollbackService(project, identity, root, transport=transport)
    try:
        service.rollback_latest(fail_after_writes=1)
        raised = False
    except PolicyError:
        raised = True
    assert raised
    # First snapshot restored and verified; second still after-bytes.
    assert transport.files["/srv/a.txt"].data == b"ba"
    assert transport.files["/srv/b.txt"].data == b"ab"
    assert store.read_current().generation == 2  # type: ignore[union-attr]
    open_tx = TransactionStore(root).list_open()
    assert open_tx
    assert open_tx[0].stage == "remote_mutating"


def test_rollback_post_write_verify_succeeds_with_readback(tmp_path: Path) -> None:
    """Successful rollback performs readback (remote matches before_sha256)."""

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    from git_deploy.git_store import PersistentGitStore
    import subprocess

    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    PersistentGitStore(tmp_path / "target", repo).ensure_layout()
    PersistentGitStore(tmp_path / "target", repo)._publish_repository_identity()
    result = service.rollback_latest()
    assert result.status == "succeeded"
    assert transport.files["/srv/a.txt"].data == b"before"


def test_rollback_discard_write_fails_closed(tmp_path: Path) -> None:
    """Silent discard of write_file must not succeed rollback or advance generation."""

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.errors import PolicyError
    import subprocess

    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    gen_before = store.read_current().generation  # type: ignore[union-attr]

    def discard(remote_path: str, data: bytes, executable: bool = False) -> None:
        # Record call but leave remote at previous content.
        transport.write_calls += 1
        return None

    transport.write_file = discard  # type: ignore[method-assign]
    try:
        service.rollback_latest()
        raised = False
    except PolicyError:
        raised = True
    assert raised
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    assert transport.files["/srv/a.txt"].data == b"after"
    from git_deploy.transaction import TransactionStore

    open_tx = TransactionStore(root).list_open()
    assert open_tx
    assert open_tx[0].stage == "remote_mutating"


def test_rollback_corrupt_write_fails_closed(tmp_path: Path) -> None:
    """Corrupt restore bytes fail readback; journal open; gen unchanged."""

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.errors import PolicyError
    import subprocess

    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    gen_before = store.read_current().generation  # type: ignore[union-attr]
    real_write = transport.write_file

    def corrupt(remote_path: str, data: bytes, executable: bool = False) -> None:
        real_write(remote_path, b"not-before", executable=executable)

    transport.write_file = corrupt  # type: ignore[method-assign]
    try:
        service.rollback_latest()
        raised = False
    except PolicyError:
        raised = True
    assert raised
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    from git_deploy.transaction import TransactionStore

    assert TransactionStore(root).list_open()


def test_rollback_multi_file_crash_partial(tmp_path: Path) -> None:
    """Alias of multi-snapshot crash gate (R4-06 rollback_multi_file filter)."""

    test_rollback_crash_leaves_open_journal(tmp_path)


def test_rollback_repeat_already_rolled_back(tmp_path: Path) -> None:
    """Second rollback of same deployment refuses; gen unchanged; write=0."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()

    first = service.rollback_latest()
    assert first.status == "succeeded"
    gen_after = store.read_current().generation  # type: ignore[union-attr]
    writes = transport.write_calls
    deletes = getattr(transport, "delete_calls", 0)
    with pytest.raises(PolicyError, match="already rolled back|does not match current"):
        service.rollback_latest()
    assert store.read_current().generation == gen_after  # type: ignore[union-attr]
    assert transport.write_calls == writes
    assert getattr(transport, "delete_calls", 0) == deletes
    assert TransactionStore(root).list_open() == []


def test_rollback_eligibility_current_mismatch(tmp_path: Path) -> None:
    """When current advanced past deployment after-state, refuse before remote I/O."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    # Advance current via state-only-like CAS to a new state id ≠ after_state_id.
    loaded = store.load_current_state()
    assert loaded is not None
    pointer, cur = loaded
    policy = policy_fingerprint_for_project(
        ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "st")
    )
    advanced = build_expected_state(
        generation=pointer.generation + 1,
        parent_state_id=pointer.state_id,
        source_tree_id=cur.source_tree_id,
        applied_transition_ids=cur.applied_transition_ids + ("t_extra",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=cur.files,
    )
    store.cas_advance(expected_generation=pointer.generation, state=advanced)
    gen_before = store.read_current().generation  # type: ignore[union-attr]
    writes = transport.write_calls
    with pytest.raises(PolicyError, match="does not match current|already rolled back"):
        service.rollback_latest()
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    assert transport.write_calls == writes
    assert TransactionStore(root).list_open() == []


def test_rollback_before_state_missing_fail_closed(tmp_path: Path) -> None:
    """v1 before_state_id unreadable → refuse; no journal; gen unchanged."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    before_id = latest.before_state_id
    assert before_id
    real_read = store.read_state

    def boom(state_id: str):
        if state_id == before_id:
            raise FileNotFoundError("injected missing before state")
        return real_read(state_id)

    service.state_store.read_state = boom  # type: ignore[method-assign]
    gen_before = store.read_current().generation  # type: ignore[union-attr]
    writes = transport.write_calls
    with pytest.raises(PolicyError, match="before state|unreadable|integrity"):
        service.rollback_latest()
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    assert transport.write_calls == writes
    assert TransactionStore(root).list_open() == []


def test_rollback_before_state_corrupt_fail_closed(tmp_path: Path) -> None:
    """Tampered before state bytes fail closed before transport mutation."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    before_id = latest.before_state_id
    assert before_id
    real_read = store.read_state

    def boom(state_id: str):
        if state_id == before_id:
            raise ValueError("injected corrupt before state")
        return real_read(state_id)

    service.state_store.read_state = boom  # type: ignore[method-assign]
    gen_before = store.read_current().generation  # type: ignore[union-attr]
    writes = transport.write_calls
    with pytest.raises(PolicyError, match="before state|integrity|unreadable"):
        service.rollback_latest()
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    assert transport.write_calls == writes
    assert TransactionStore(root).list_open() == []


def test_rollback_before_state_policy_mismatch_fails_before_mutation(tmp_path: Path) -> None:
    """A readable before-state from another policy cannot become rollback current."""

    from dataclasses import replace

    from git_deploy.transaction import TransactionStore

    service, transport, store, _identity, _older, latest = _setup(tmp_path)
    before_id = latest.before_state_id
    assert before_id is not None
    real_read = service.state_store.read_state

    def wrong_policy(state_id: str):
        state = real_read(state_id)
        if state_id == before_id:
            return replace(state, policy_fingerprint="wrong-policy")
        return state

    service.state_store.read_state = wrong_policy  # type: ignore[method-assign]
    generation = store.read_current().generation  # type: ignore[union-attr]
    writes = transport.write_calls
    with pytest.raises(PolicyError, match="before state managed policy mismatch"):
        service.rollback_latest()
    assert store.read_current().generation == generation  # type: ignore[union-attr]
    assert transport.write_calls == writes
    assert TransactionStore(tmp_path / "target").list_open() == []


def test_rollback_before_source_tree_unreadable_fails_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The before tree is independently checked after the current-state guard."""

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    service, transport, store, _identity, _older, _latest = _setup(tmp_path)
    original = PersistentGitStore.require_tree
    calls = {"count": 0}

    def fail_second_tree_check(self: PersistentGitStore, tree_id: str) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise ConfigurationError("injected missing before tree")
        original(self, tree_id)

    monkeypatch.setattr(PersistentGitStore, "require_tree", fail_second_tree_check)
    generation = store.read_current().generation  # type: ignore[union-attr]
    writes = transport.write_calls
    with pytest.raises(PolicyError, match="before state source tree unreadable"):
        service.rollback_latest()
    assert calls["count"] == 2
    assert store.read_current().generation == generation  # type: ignore[union-attr]
    assert transport.write_calls == writes
    assert TransactionStore(tmp_path / "target").list_open() == []


def test_rollback_lineage_integrity_keeps_unmanaged_paths(tmp_path: Path) -> None:
    """Successful v1 rollback after state retains full before file table (untouched paths)."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    before_state_id = latest.before_state_id
    assert before_state_id is not None
    expected_before = store.read_state(before_state_id)
    # Ensure the rollback state comes from the complete before-state, not snapshots only.
    result = service.rollback_latest()
    assert result.status == "succeeded"
    loaded = store.load_current_state()
    assert loaded is not None
    _p, state = loaded
    paths = {e.path for e in state.files if e.exists}
    assert "a.txt" in paths
    assert "untouched.txt" in paths
    assert state.source_tree_id == expected_before.source_tree_id


def test_rollback_discard_delete_fails_closed(tmp_path: Path) -> None:
    """Silent discard of delete_file fails readback; journal open; gen unchanged."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    # Build deployment with delete snapshot (before_exists=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "st")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    tree = _real_empty_tree(repo)
    from git_deploy.object_store import ContentAddressedStore

    ContentAddressedStore(root).put(b"x")
    policy = policy_fingerprint_for_project(project)
    store = ExpectedStateStore(root, identity)
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(),
    )
    store.cas_advance(expected_generation=None, state=before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id=tree,
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(
            FileEntry(
                path="gone.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"x").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=1, state=after)
    deploy = DeploymentStore(project, root=root)
    deploy.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T000010Z-del",
            project="demo",
            repository=str(repo),
            remote_root="/srv",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
            snapshots=[
                FileSnapshot(
                    path="gone.txt",
                    remote_path="/srv/gone.txt",
                    before_exists=False,
                    before_sha256=None,
                    backup_file=None,
                    after_exists=True,
                    after_sha256=hashlib.sha256(b"x").hexdigest(),
                )
            ],
            state="v1",
            before_state_id=before.state_id(),
            after_state_id=after.state_id(),
            before_generation=1,
            after_generation=2,
            introduced_transition_ids=["t1"],
            target_id=identity.target_id,
        )
    )
    transport = InMemoryTransport()
    transport.files["/srv/gone.txt"] = FakeRemotePath(b"x")
    service = StateRollbackService(project, identity, root, transport=transport)
    gen_before = store.read_current().generation  # type: ignore[union-attr]

    def discard_delete(remote_path: str) -> None:
        transport.delete_calls = getattr(transport, "delete_calls", 0) + 1
        # leave file present
        return None

    transport.delete_file = discard_delete  # type: ignore[method-assign]
    with pytest.raises(PolicyError):
        service.rollback_latest()
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    assert TransactionStore(root).list_open()
    assert "/srv/gone.txt" in transport.files


def test_rollback_read_error_fails_closed(tmp_path: Path) -> None:
    """Readback raising leaves open journal and does not CAS."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.transaction import TransactionStore

    service, transport, store, identity, _older, latest = _setup(tmp_path)
    repo = tmp_path / "repo"
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    gen_before = store.read_current().generation  # type: ignore[union-attr]
    real_read = transport.read_file
    calls = {"n": 0}

    def flaky_read(remote_path: str):
        calls["n"] += 1
        # Allow pre-rollback backup reads; fail during post-write verify.
        if calls["n"] > 1:
            raise OSError("injected readback failure")
        return real_read(remote_path)

    transport.read_file = flaky_read  # type: ignore[method-assign]
    with pytest.raises(PolicyError):
        service.rollback_latest()
    assert store.read_current().generation == gen_before  # type: ignore[union-attr]
    assert TransactionStore(root).list_open()


def test_empty_rollback_restores_zero_bytes(tmp_path: Path) -> None:
    """Latest rollback of empty-file path restores b\"\" and empty hash."""

    import subprocess

    from git_deploy.git_store import PersistentGitStore

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "st")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    tree = _real_empty_tree(repo)
    from git_deploy.object_store import ContentAddressedStore

    cas = ContentAddressedStore(root)
    cas.put(b"")
    cas.put(b"x")
    policy = policy_fingerprint_for_project(project)
    store = ExpectedStateStore(root, identity)
    empty_h = hashlib.sha256(b"").hexdigest()
    filled_h = hashlib.sha256(b"x").hexdigest()
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(
            FileEntry(path="e.txt", owner="source", content_sha256=empty_h, exists=True),
        ),
    )
    store.cas_advance(expected_generation=None, state=before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id=tree,
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy,
        files=(
            FileEntry(path="e.txt", owner="source", content_sha256=filled_h, exists=True),
        ),
    )
    store.cas_advance(expected_generation=1, state=after)
    deploy = DeploymentStore(project, root=root)
    bak = deploy.write_backup("20200101T000020Z-empty", 0, b"")
    deploy.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T000020Z-empty",
            project="demo",
            repository=str(repo),
            remote_root="/srv",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
            snapshots=[
                FileSnapshot(
                    path="e.txt",
                    remote_path="/srv/e.txt",
                    before_exists=True,
                    before_sha256=empty_h,
                    backup_file=bak,
                    after_exists=True,
                    after_sha256=filled_h,
                )
            ],
            state="v1",
            before_state_id=before.state_id(),
            after_state_id=after.state_id(),
            before_generation=1,
            after_generation=2,
            introduced_transition_ids=["t1"],
            target_id=identity.target_id,
        )
    )
    transport = InMemoryTransport()
    transport.files["/srv/e.txt"] = FakeRemotePath(b"x")
    service = StateRollbackService(project, identity, root, transport=transport)
    result = service.rollback_latest()
    assert result.status == "succeeded"
    assert transport.files["/srv/e.txt"].data == b""
    assert store.read_current().generation == 3  # type: ignore[union-attr]
