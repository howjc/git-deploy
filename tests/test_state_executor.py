"""Stateful deploy executor tests (D-series)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_deploy.errors import PolicyError, RemoteDriftError
from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
from git_deploy.models import PlannedFile, ProjectConfig
from git_deploy.state_executor import InMemoryTransport, StateDeploymentExecutor
from git_deploy.state_planner import SourceDiffPlan
from git_deploy.target_identity import resolve_target_identity
from git_deploy.transaction import TransactionStore


def _init_git_store(root: Path, repo: Path) -> str:
    """Initialize durable git store and return empty tree id for seeds.

    Args:
        root: Target root.
        repo: Repository path (created as git repo if needed).

    Returns:
        Empty tree object id readable via the store alternates.
    """

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository

    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    store = PersistentGitStore(root, repo)
    store.ensure_layout()
    store._publish_repository_identity()
    empty = GitRepository(repo).empty_tree()
    # Empty tree lives in main repo; require_tree should succeed via alternates.
    try:
        store.require_tree(empty)
    except Exception:
        pass
    return empty


def _setup(tmp_path: Path):
    """Create project/identity/executor with empty current.

    Args:
        tmp_path: Temp root.

    Returns:
        executor, transport, identity, root.
    """

    repo = tmp_path / "repo"
    root = tmp_path / "target"
    tree = _init_git_store(root, repo)
    project = ProjectConfig(
        name="demo",
        repository=repo,
        remote_root="/srv",
        local_state_dir=tmp_path / "legacy-state",
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    transport = InMemoryTransport()
    contents = {"a.txt": b"new-a", "b.txt": b"new-b"}
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=transport,
        content_provider=lambda p: contents.get(p, b""),
    )
    executor._seed_tree = tree  # type: ignore[attr-defined]
    return executor, transport, identity, root, project


def _file(
    path: str,
    *,
    before: str | None,
    after: str | None,
    action: str = "upload",
) -> PlannedFile:
    """Build a planned file.

    Args:
        path: Relative path.
        before: Before hash.
        after: After hash.
        action: Action.

    Returns:
        PlannedFile.
    """

    return PlannedFile(
        action=action,
        path=path,
        remote_path=f"/srv/{path}",
        source_path=path if action != "delete" else None,
        expected_before_sha256=before,
        target_sha256=after,
        target_size=0,
        executable=False,
    )


def _plan(introduced: tuple[str, ...] = ("t1",), tree: str = "tree-after") -> SourceDiffPlan:
    """Minimal source plan.

    Args:
        introduced: Introduced transitions.
        tree: After tree id.

    Returns:
        SourceDiffPlan.
    """

    return SourceDiffPlan(
        before_tree_id="tree-before",
        after_tree_id=tree,
        files=(),
        excluded=(),
        introduced_transition_ids=introduced,
        applied_transition_ids=("t0",) + introduced,
    )


def _seed_current(root: Path, identity, generation: int = 1, transitions: tuple[str, ...] = ("t0",)):
    """Write a generation-1 current state.

    Args:
        root: Target root.
        identity: Identity.
        generation: Generation.
        transitions: Applied transitions.

    Returns:
        Expected state.
    """

    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.target_identity import policy_fingerprint_for_project

    store = ExpectedStateStore(root, identity)
    project = ProjectConfig(name="demo", repository=root.parent / "repo", remote_root="/srv")
    tree = _init_git_store(root, root.parent / "repo")
    ContentAddressedStore(root).put(b"old-a")
    state = build_expected_state(
        generation=generation,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=transitions,
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry(path="a.txt", owner="source", content_sha256=hashlib.sha256(b"old-a").hexdigest()),),
    )
    store.cas_advance(expected_generation=None if generation == 1 else generation - 1, state=state)
    return state


def test_drift_current_target_and_third(tmp_path: Path) -> None:
    """actual=current ok, actual=target satisfied, third content blocks."""

    executor, transport, identity, root, _project = _setup(tmp_path)
    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    from git_deploy.state_executor import FakeRemotePath

    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    files = [_file("a.txt", before=old, after=new)]
    decisions = executor.evaluate_drift(files)
    assert decisions[0].status == "ok"

    transport.files["/srv/a.txt"] = FakeRemotePath(b"new-a")
    decisions = executor.evaluate_drift(files)
    assert decisions[0].status == "satisfied"

    transport.files["/srv/a.txt"] = FakeRemotePath(b"third")
    decisions = executor.evaluate_drift(files)
    assert decisions[0].status == "drift"
    with pytest.raises(RemoteDriftError, match="current"):
        executor.reject_third_content(decisions)


def test_prepared_before_first_transport_write(tmp_path: Path) -> None:
    """After state, backups, prepared journal are durable before transport writes."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    files = [_file("a.txt", before=old, after=new)]
    plan = _plan()
    before = ExpectedStateStore(root, identity).load_current_state()
    assert before is not None
    after = executor._build_after_state(plan, before[1], files)
    writes_before = transport.write_calls
    journal = executor.prepare(
        plan=plan,
        files=files,
        before_state=before[1],
        after_state=after,
        deployment_id="dep1",
    )
    assert journal.stage == "prepared"
    assert transport.write_calls == writes_before
    assert (root / "transactions" / f"{journal.transaction_id}.json").is_file()


def test_commit_state_advances_generation(tmp_path: Path) -> None:
    """Successful deploy CAS-advances generation."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    result = executor.deploy(_plan(), [_file("a.txt", before=old, after=new)])
    assert result["status"] == "succeeded"
    assert result["generation"] == 2
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None and current.generation == 2


def test_auto_restore_state_keeps_before(tmp_path: Path) -> None:
    """Partial upload then failure restores before bytes from durable backups."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    # Hook content provider so we can observe the partial mutation.
    mutated: list[bytes] = []
    original_provider = executor.content_provider

    def tracking_provider(path: str) -> bytes:
        data = original_provider(path)
        mutated.append(data)
        return data

    executor.content_provider = tracking_provider
    result = executor.deploy(
        _plan(),
        [_file("a.txt", before=old, after=new)],
        fail_at="upload",
    )
    assert result["status"] == "restored"
    assert result["generation"] == 1
    # Partial mutation must have written new bytes before the injected failure.
    assert mutated and mutated[0] == b"new-a"
    # Durable backup restore must put old bytes back (not merely never-written).
    assert transport.files["/srv/a.txt"].data == b"old-a"
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None and current.generation == 1
    # Journal backup_entries must be the restore source of truth.
    from git_deploy.transaction import TransactionStore

    journals = TransactionStore(root).list_all()
    assert journals
    assert journals[0].meta.get("backup_entries")


def test_manual_recovery_required_blocks_next_deploy(tmp_path: Path) -> None:
    """Failed restore leaves manual gate that blocks the next deploy."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    tx = TransactionStore(root)
    journal = tx.create(
        target_id=identity.target_id,
        stage="prepared",
        before_generation=1,
        after_generation=2,
    )
    tx.advance(journal, "manual_recovery_required", error="restore failed")
    from git_deploy.state_executor import FakeRemotePath

    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    with pytest.raises(PolicyError, match="transaction|manual"):
        executor.deploy(_plan(), [_file("a.txt", before=old, after=new)])


def test_repeated_noop_zero_writes(tmp_path: Path) -> None:
    """Matching current/target returns already deployed with zero writes."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    transport.files["/srv/a.txt"] = FakeRemotePath(b"new-a")
    new = hashlib.sha256(b"new-a").hexdigest()
    plan = SourceDiffPlan(
        before_tree_id="t",
        after_tree_id="t",
        files=(),
        excluded=(),
        introduced_transition_ids=(),
        applied_transition_ids=("t0",),
        static_noop=True,
        remote_unverified=False,
    )
    writes = transport.write_calls
    result = executor.deploy(plan, [_file("a.txt", before=new, after=new)])
    assert result["status"] == "already deployed"
    assert transport.write_calls == writes


def test_partial_target_filters_satisfied(tmp_path: Path) -> None:
    """Only unsatisfied paths are mutated; after state expresses full target."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    old = hashlib.sha256(b"old-a").hexdigest()
    new_a = hashlib.sha256(b"new-a").hexdigest()
    new_b = hashlib.sha256(b"new-b").hexdigest()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    transport.files["/srv/b.txt"] = FakeRemotePath(b"new-b")  # already target
    files = [
        _file("a.txt", before=old, after=new_a),
        _file("b.txt", before=new_b, after=new_b),
    ]
    decisions = executor.evaluate_drift(files)
    effective = executor.filter_effective(files, decisions)
    assert [item.path for item in effective] == ["a.txt"]


def test_reconciliation_state_only(tmp_path: Path) -> None:
    """State-only transition advances generation with zero remote writes."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    plan = _plan(introduced=("t1",))
    writes = transport.write_calls
    result = executor.deploy(plan, [])
    assert result["status"] == "reconciled"
    assert result["generation"] == 2
    assert transport.write_calls == writes


def test_full_snapshot_retains_unchanged_managed_paths(tmp_path: Path) -> None:
    """After state retains unchanged managed files when only one path mutates."""

    executor, transport, identity, root, _p = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_executor import FakeRemotePath

    ContentAddressedStore(root).put(b"old-b")
    # Extend current to include b.txt as well.
    store = ExpectedStateStore(root, identity)
    pointer, before = store.load_current_state()  # type: ignore[misc]
    assert pointer is not None
    from git_deploy.target_identity import policy_fingerprint_for_project

    project = ProjectConfig(name="demo", repository=root.parent / "repo", remote_root="/srv")
    dual = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=_init_git_store(root, root.parent / "repo"),
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(path="a.txt", owner="source", content_sha256=hashlib.sha256(b"old-a").hexdigest()),
            FileEntry(path="b.txt", owner="source", content_sha256=hashlib.sha256(b"old-b").hexdigest()),
        ),
    )
    # rewrite gen1
    import shutil

    shutil.rmtree(root)
    tree = _init_git_store(root, root.parent / "repo")
    dual = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(path="a.txt", owner="source", content_sha256=hashlib.sha256(b"old-a").hexdigest()),
            FileEntry(path="b.txt", owner="source", content_sha256=hashlib.sha256(b"old-b").hexdigest()),
        ),
    )
    store = ExpectedStateStore(root, identity)
    ContentAddressedStore(root).put(b"old-a")
    ContentAddressedStore(root).put(b"old-b")
    store.cas_advance(expected_generation=None, state=dual)
    executor.state_store = store
    executor.tx_store = TransactionStore(root)
    executor.cas = ContentAddressedStore(root)
    from git_deploy.state import DeploymentStore

    executor.deploy_store = DeploymentStore(project, root=root)

    old_a = hashlib.sha256(b"old-a").hexdigest()
    new_a = hashlib.sha256(b"new-a").hexdigest()
    old_b = hashlib.sha256(b"old-b").hexdigest()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    transport.files["/srv/b.txt"] = FakeRemotePath(b"old-b")
    files = [
        _file("a.txt", before=old_a, after=new_a),
        _file("b.txt", before=old_b, after=old_b),
    ]
    plan = _plan(introduced=("t1",))
    result = executor.deploy(plan, files)
    assert result["status"] == "succeeded"
    loaded = store.load_current_state()
    assert loaded is not None
    _p2, after = loaded
    paths = {entry.path for entry in after.files if entry.exists}
    assert "a.txt" in paths
    assert "b.txt" in paths  # unchanged managed path retained
    assert after.files[0].path == "a.txt" or any(e.path == "b.txt" for e in after.files)


def test_target_scoped_deployment_store_isolates_dev_prod(tmp_path: Path) -> None:
    """Distinct physical targets use isolated deployment/backup roots."""

    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "legacy")
    id_dev = resolve_target_identity({"protocol": "sftp", "host": "dev.h"}, project)
    id_prod = resolve_target_identity({"protocol": "sftp", "host": "prod.h"}, project)
    root_dev = tmp_path / "t-dev"
    root_prod = tmp_path / "t-prod"
    from git_deploy.state import DeploymentStore

    store_dev = DeploymentStore(project, root=root_dev)
    store_prod = DeploymentStore(project, root=root_prod)
    store_dev.write_backup("20200101T000000Z-dev1", 0, b"dev-backup")
    store_prod.write_backup("20200101T000000Z-prod1", 0, b"prod-backup")
    assert store_dev.root != store_prod.root
    assert (root_dev / "deployments").is_dir()
    assert (root_prod / "deployments").is_dir()
    assert not (root_dev / "deployments" / "20200101T000000Z-prod1").exists()
    assert store_dev.read_backup("20200101T000000Z-dev1", "backups/00000.bin") == b"dev-backup"
    assert id_dev.target_id != id_prod.target_id


def test_rollback_roundtrip_from_real_deploy_manifest(tmp_path: Path) -> None:
    """StateRollbackService restores bytes/transitions from a real deploy manifest."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath
    from git_deploy.state_rollback import StateRollbackService

    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    loaded = ExpectedStateStore(root, identity).load_current_state()
    assert loaded is not None
    _pointer, before_state = loaded
    # A rollback-eligible real deployment must retain a tree readable through
    # the persistent Git store; synthetic fixture labels are intentionally invalid.
    plan = _plan(introduced=("t1",), tree=before_state.source_tree_id)
    files = [_file("a.txt", before=old, after=new)]
    result = executor.deploy(plan, files)
    assert result["status"] == "succeeded"
    assert transport.files["/srv/a.txt"].data == b"new-a"
    gen_after = result["generation"]

    service = StateRollbackService(project, identity, root, transport=transport)
    rolled = service.rollback_latest()
    assert rolled.status == "succeeded"
    assert transport.files["/srv/a.txt"].data == b"old-a"
    assert rolled.generation == gen_after + 1
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None
    state = ExpectedStateStore(root, identity).read_state(current.state_id)
    assert "t1" not in state.applied_transition_ids


def test_stateful_deploy_post_commands_use_transport_execute(tmp_path: Path) -> None:
    """Product hook wiring must call transport.execute (SFTP surface), not only run_command."""

    from dataclasses import dataclass, field

    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_executor import FakeRemotePath, StateDeploymentExecutor
    from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity

    @dataclass
    class ExecuteOnlyTransport:
        """Mimics product SftpTransport: execute only, no run_command alias."""

        files: dict[str, FakeRemotePath] = field(default_factory=dict)
        write_calls: int = 0
        delete_calls: int = 0
        read_calls: int = 0
        supports_commands: bool = True
        execute_calls: list[str] = field(default_factory=list)

        def read_file(self, remote_path: str) -> bytes | None:
            self.read_calls += 1
            item = self.files.get(remote_path)
            return None if item is None else item.data

        def write_file(self, remote_path: str, data: bytes, executable: bool = False) -> None:
            self.write_calls += 1
            self.files[remote_path] = FakeRemotePath(data=data, executable=executable)

        def delete_file(self, remote_path: str) -> None:
            self.delete_calls += 1
            self.files.pop(remote_path, None)

        def execute(self, command: str) -> tuple[int, str, str]:
            self.execute_calls.append(command)
            return 0, "ok", ""

        def close(self) -> None:
            return None

    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectConfig(
        name="demo",
        repository=repo,
        remote_root="/srv",
        local_state_dir=tmp_path / "legacy-state",
        post_commands=("echo-ok",),
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    transport = ExecuteOnlyTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    ContentAddressedStore(root).put(b"old-a")
    ContentAddressedStore(root).put(b"new-a")
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=_init_git_store(root, root.parent / "repo"),
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry(path="a.txt", owner="source", content_sha256=hashlib.sha256(b"old-a").hexdigest()),),
    )
    store.cas_advance(expected_generation=None, state=state)
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=transport,
        content_provider=lambda p: b"new-a",
    )
    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    result = executor.deploy(_plan(introduced=("t1",)), [_file("a.txt", before=old, after=new)])
    assert result["status"] == "succeeded"
    assert transport.execute_calls == ["echo-ok"]


def test_post_write_verify_detects_hash_mismatch(tmp_path: Path) -> None:
    """Post-write read-back fails when transport returns wrong bytes."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    real_write = transport.write_file
    writes = {"n": 0}

    def corrupt_once(remote_path: str, data: bytes, executable: bool = False) -> None:
        writes["n"] += 1
        if writes["n"] == 1:
            # First mutation write is corrupted; later restore writes are honest.
            real_write(remote_path, b"corrupted-not-new", executable=executable)
        else:
            real_write(remote_path, data, executable=executable)

    transport.write_file = corrupt_once  # type: ignore[method-assign]
    plan = _plan(introduced=("t1",))
    result = executor.deploy(plan, [_file("a.txt", before=old, after=new)])
    assert result["status"] == "restored"
    assert transport.files["/srv/a.txt"].data == b"old-a"


def test_content_digest_mismatch_fail_closed(tmp_path: Path) -> None:
    """prepare fails closed when content provider digest != planned target hash."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath

    old = hashlib.sha256(b"old-a").hexdigest()
    new = hashlib.sha256(b"new-a").hexdigest()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    executor.content_provider = lambda p: b"wrong-bytes"
    plan = _plan(introduced=("t1",))
    try:
        result = executor.deploy(plan, [_file("a.txt", before=old, after=new)])
        assert result.get("status") != "succeeded"
    except Exception as exc:
        assert "digest mismatch" in str(exc).lower() or "mismatch" in str(exc).lower()


def test_empty_file_deploy_and_cas(tmp_path: Path) -> None:
    """Zero-byte managed file completes prepare, CAS, upload, and current."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.state_executor import FakeRemotePath
    from git_deploy.object_store import ContentAddressedStore

    empty_hash = hashlib.sha256(b"").hexdigest()
    old = hashlib.sha256(b"old-a").hexdigest()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"old-a")
    # Add empty new file empty.txt and change a to empty.
    executor.content_provider = lambda p: b"" if p in {"a.txt", "empty.txt"} else b"x"
    ContentAddressedStore(root).put(b"")
    files = [
        _file("a.txt", before=old, after=empty_hash),
        PlannedFile(
            action="upload",
            path="empty.txt",
            remote_path="/srv/empty.txt",
            source_path="empty.txt",
            expected_before_sha256=None,
            target_sha256=empty_hash,
            target_size=0,
        ),
    ]
    plan = _plan(introduced=("t1",))
    result = executor.deploy(plan, files)
    assert result["status"] == "succeeded"
    assert transport.files["/srv/a.txt"].data == b""
    assert transport.files["/srv/empty.txt"].data == b""
    assert ContentAddressedStore(root).contains(empty_hash)


def test_state_only_checkpoint_cas_before_recovered(tmp_path: Path) -> None:
    """State-only journal is non-terminal until CAS, then recovered."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.transaction import TransactionStore

    plan = _plan(introduced=("t1",))
    result = executor.deploy(plan, [])
    assert result["status"] == "reconciled"
    open_tx = TransactionStore(root).list_open()
    assert open_tx == []
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None
    assert current.generation == 2

def test_state_only_crash_leaves_open_when_cas_fails(tmp_path: Path) -> None:
    """If CAS fails after state write, journal stays non-terminal (prepared)."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.transaction import TransactionStore
    from git_deploy.errors import ConfigurationError

    plan = _plan(introduced=("t1",))

    def boom(**kwargs):
        raise ConfigurationError("injected cas failure")

    executor.state_store.cas_advance = boom  # type: ignore[method-assign]
    try:
        executor.deploy(plan, [])
        raised = False
    except Exception:
        raised = True
    assert raised
    open_tx = TransactionStore(root).list_open()
    assert open_tx
    assert open_tx[0].stage == "prepared"
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None
    assert current.generation == 1


def test_reconciliation_recover_after_state_only(tmp_path: Path) -> None:
    """Crash before CAS leaves prepared journal; recover once CAS-advances gen, write=0.

    Ships: StateDeploymentExecutor._state_only → TransactionRecoveryService.execute.
    """

    from git_deploy.transaction import TransactionStore
    from git_deploy.transaction_recovery import TransactionRecoveryService
    from git_deploy.errors import ConfigurationError

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    plan = _plan(introduced=("t1",))

    def boom(**kwargs):
        raise ConfigurationError("injected cas failure")

    executor.state_store.cas_advance = boom  # type: ignore[method-assign]
    try:
        executor.deploy(plan, [])
        raised = False
    except Exception:
        raised = True
    assert raised
    open_tx = TransactionStore(root).list_open()
    assert open_tx
    assert open_tx[0].stage == "prepared"
    assert open_tx[0].meta.get("kind") == "state_only"
    assert open_tx[0].after_state_id is not None
    gen_before = ExpectedStateStore(root, identity).read_current().generation  # type: ignore[union-attr]
    assert gen_before == 1
    writes_before = transport.write_calls

    # Recover: service reopens stores and CAS-finalizes prepared state_only after_id.
    svc = TransactionRecoveryService(root, identity)
    journal = open_tx[0]
    decision = svc.decide_for_journal(journal)
    assert decision.decision == "restore"
    updated = svc.execute(decision, journal)
    assert updated.stage == "recovered"
    assert TransactionStore(root).list_open() == []
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None
    assert current.generation == 2  # single CAS advance via recover
    assert transport.write_calls == writes_before  # remote write=0


def test_empty_file_extended_roundtrip_and_mismatch(tmp_path: Path) -> None:
    """Empty→non-empty, digest mismatch, and true provider missing fail-closed."""

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_executor import FakeRemotePath
    from git_deploy.models import PlannedFile
    from git_deploy.errors import ConfigurationError

    empty_hash = hashlib.sha256(b"").hexdigest()
    filled = hashlib.sha256(b"full").hexdigest()
    ContentAddressedStore(root).put(b"")
    ContentAddressedStore(root).put(b"full")
    transport.files["/srv/empty.txt"] = FakeRemotePath(b"")
    # empty → non-empty
    executor.content_provider = lambda p: b"full"
    plan = _plan(introduced=("t1",))
    files = [
        PlannedFile(
            action="upload",
            path="empty.txt",
            remote_path="/srv/empty.txt",
            source_path="empty.txt",
            expected_before_sha256=empty_hash,
            target_sha256=filled,
            target_size=4,
        )
    ]
    result = executor.deploy(plan, files)
    assert result["status"] == "succeeded"
    assert transport.files["/srv/empty.txt"].data == b"full"

    # digest mismatch fail-closed
    executor.content_provider = lambda p: b"wrong"
    plan2 = _plan(introduced=("t2",), tree="tree-after-2")
    try:
        executor.deploy(
            plan2,
            [
                PlannedFile(
                    action="upload",
                    path="empty.txt",
                    remote_path="/srv/empty.txt",
                    source_path="empty.txt",
                    expected_before_sha256=filled,
                    target_sha256=empty_hash,
                    target_size=0,
                )
            ],
        )
        raised = False
    except Exception:
        raised = True
    assert raised

    # true provider missing via exception
    def missing(path: str) -> bytes:
        raise ConfigurationError(f"provider missing for {path}")

    executor.content_provider = missing
    plan3 = _plan(introduced=("t3",), tree="tree-after-3")
    ContentAddressedStore(root).put(b"")
    try:
        executor.deploy(
            plan3,
            [
                PlannedFile(
                    action="upload",
                    path="newempty.txt",
                    remote_path="/srv/newempty.txt",
                    source_path="newempty.txt",
                    expected_before_sha256=None,
                    target_sha256=empty_hash,
                    target_size=0,
                )
            ],
        )
        missing_raised = False
    except Exception:
        missing_raised = True
    assert missing_raised


def test_state_only_post_cas_crash_and_recover(tmp_path: Path) -> None:
    """CAS succeeded then crash before terminal journal; recover once, write=0."""

    from git_deploy.transaction import TransactionStore
    from git_deploy.transaction_recovery import TransactionRecoveryService

    executor, transport, identity, root, project = _setup(tmp_path)
    _seed_current(root, identity)
    plan = _plan(introduced=("t1",))
    executor._fail_after_state_only_cas = True  # type: ignore[attr-defined]
    try:
        executor.deploy(plan, [])
        raised = False
    except Exception:
        raised = True
    assert raised
    open_tx = TransactionStore(root).list_open()
    assert open_tx
    assert open_tx[0].stage == "prepared"
    assert open_tx[0].after_state_id is not None
    # CAS already advanced generation despite open journal.
    gen = ExpectedStateStore(root, identity).read_current().generation  # type: ignore[union-attr]
    assert gen == 2
    writes = transport.write_calls
    svc = TransactionRecoveryService(root, identity)
    decision = svc.decide_for_journal(open_tx[0])
    updated = svc.execute(decision, open_tx[0])
    assert updated.stage == "recovered"
    assert TransactionStore(root).list_open() == []
    assert ExpectedStateStore(root, identity).read_current().generation == 2  # type: ignore[union-attr]
    assert transport.write_calls == writes
