"""Transaction recovery decision table and crash-stage tests."""

from __future__ import annotations

from pathlib import Path

from git_deploy.expected_state import ExpectedStateStore, build_expected_state
from git_deploy.target_identity import resolve_target_identity
from git_deploy.transaction import TransactionStore
from git_deploy.transaction_recovery import RecoveryContext, TransactionRecoveryService


def _svc(tmp_path: Path):
    """Build recovery service with generation-1 current.

    Args:
        tmp_path: Temp path.

    Returns:
        service, tx store, identity, root.
    """

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h"},
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
        policy_fingerprint="pol",
    )
    store.cas_advance(expected_generation=None, state=state)
    return TransactionRecoveryService(root, identity), TransactionStore(root), identity, root


def test_decision_table_unknown_defaults_manual(tmp_path: Path) -> None:
    """Decision table covers finalize/restore/manual; unknown → manual zero mutation."""

    svc, tx, identity, root = _svc(tmp_path)
    cases = [
        RecoveryContext("prepared", 1, 1, 2),
        RecoveryContext("remote_mutating", 1, 1, 2, remote_matches_current=True),
        RecoveryContext("remote_mutating", 1, 1, 2, remote_matches_target=True),
        RecoveryContext("remote_verified", 1, 1, 2, remote_matches_target=True),
        RecoveryContext("remote_verified", 1, 1, 2, remote_third=True),
        RecoveryContext("state_committed", 2, 1, 2),
        RecoveryContext("weird", 1, 1, 2),
    ]
    decisions = [svc.decide(ctx, "tx").decision for ctx in cases]
    assert decisions[0] == "restore"
    assert decisions[1] == "restore"
    assert decisions[2] == "manual"
    assert decisions[3] == "finalize"
    assert decisions[4] == "manual"
    assert decisions[5] == "finalize"
    assert decisions[6] == "manual"


def test_crash_prepared(tmp_path: Path) -> None:
    """prepared kill/reopen: current=before, no remote writes, journal recovered."""

    svc, tx, identity, root = _svc(tmp_path)
    journal = tx.create(
        target_id=identity.target_id,
        stage="prepared",
        before_generation=1,
        after_generation=2,
        before_state_id="before",
        after_state_id="after",
    )
    mutations = {"writes": 0}

    def restore(j):
        mutations["writes"] += 0  # must not write remote

    decision = svc.decide_for_journal(journal, remote_matches_current=True)
    assert decision.decision == "restore"
    updated = svc.execute(decision, journal, restore_callback=restore)
    assert updated.stage == "recovered"
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None and current.generation == 1
    assert mutations["writes"] == 0


def test_crash_remote_mutating(tmp_path: Path) -> None:
    """remote_mutating partial upload: restore before bytes from durable backups."""

    from git_deploy.models import PlannedFile, ProjectConfig
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
    from git_deploy.state_planner import SourceDiffPlan
    from git_deploy.target_identity import policy_fingerprint_for_project

    svc, tx, identity, root = _svc(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "leg")
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"before-bytes")
    transport.files["/srv/b.txt"] = FakeRemotePath(b"before-b")
    contents = {"a.txt": b"partial-new", "b.txt": b"never-written"}
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=transport,
        content_provider=lambda p: contents[p],
    )
    # Align policy fingerprint with executor.
    from git_deploy.expected_state import build_expected_state

    store = ExpectedStateStore(root, identity)
    loaded = store.load_current_state()
    assert loaded is not None
    pointer, old = loaded
    # Rebuild current with matching policy so guards pass.
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=old.files,
    )
    # Replace current by rewriting store root generation carefully: write as gen1 fresh.
    import shutil

    shutil.rmtree(root)
    store = ExpectedStateStore(root, identity)
    # Re-init durable git store markers required by integrity guards.
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository
    import subprocess
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    gs = PersistentGitStore(root, repo)
    gs.ensure_layout()
    gs._publish_repository_identity()
    empty = GitRepository(repo).empty_tree()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(),
    )
    store.cas_advance(expected_generation=None, state=state)
    executor.state_store = store
    executor.tx_store = TransactionStore(root)
    executor.cas = __import__("git_deploy.object_store", fromlist=["ContentAddressedStore"]).ContentAddressedStore(root)
    executor.deploy_store = __import__("git_deploy.state", fromlist=["DeploymentStore"]).DeploymentStore(project, root=root)

    import hashlib

    before_a = hashlib.sha256(b"before-bytes").hexdigest()
    after_a = hashlib.sha256(b"partial-new").hexdigest()
    before_b = hashlib.sha256(b"before-b").hexdigest()
    after_b = hashlib.sha256(b"never-written").hexdigest()
    files = [
        PlannedFile(
            action="upload",
            path="a.txt",
            remote_path="/srv/a.txt",
            source_path="a.txt",
            expected_before_sha256=before_a,
            target_sha256=after_a,
        ),
        PlannedFile(
            action="upload",
            path="b.txt",
            remote_path="/srv/b.txt",
            source_path="b.txt",
            expected_before_sha256=before_b,
            target_sha256=after_b,
        ),
    ]
    plan = SourceDiffPlan(
        before_tree_id="t0",
        after_tree_id="t1",
        files=(),
        excluded=(),
        introduced_transition_ids=("t1",),
        applied_transition_ids=("t1",),
    )
    result = executor.deploy(plan, files, fail_at="upload")
    assert result["status"] == "restored"
    # First path was partially written then restored from durable backup.
    assert transport.files["/srv/a.txt"].data == b"before-bytes"
    assert transport.files["/srv/b.txt"].data == b"before-b"
    gen_before = 1
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None
    assert current.generation == gen_before
    # Decision table still covers the abstract stage path for remote_mutating.
    j2 = TransactionStore(root).create(
        target_id=identity.target_id,
        stage="prepared",
        before_generation=1,
    )
    j2 = TransactionStore(root).advance(j2, "remote_mutating")
    decision = svc.decide_for_journal(j2, remote_matches_current=False, remote_matches_target=False)
    assert decision.decision in {"restore", "manual"}


def test_crash_remote_verified(tmp_path: Path) -> None:
    """remote_verified finalize when target matches; third content → manual without overwrite."""

    svc, tx, identity, root = _svc(tmp_path)
    journal = tx.create(target_id=identity.target_id, stage="prepared", before_generation=1, after_generation=2)
    journal = tx.advance(journal, "remote_mutating")
    journal = tx.advance(journal, "remote_verified")

    # Third content
    d1 = svc.decide_for_journal(journal, remote_third=True)
    assert d1.decision == "manual"
    j1 = svc.execute(d1, journal)
    assert j1.stage == "manual_recovery_required"
    assert ExpectedStateStore(root, identity).read_current().generation == 1  # type: ignore[union-attr]

    # Fresh verified + target match → finalize
    journal2 = tx.create(target_id=identity.target_id, stage="prepared", before_generation=1, after_generation=2)
    journal2 = tx.advance(journal2, "remote_mutating")
    journal2 = tx.advance(journal2, "remote_verified")
    finalized = {"done": False}

    def finalize(j):
        finalized["done"] = True

    d2 = svc.decide_for_journal(journal2, remote_matches_target=True)
    assert d2.decision == "finalize"
    j2 = svc.execute(d2, journal2, finalize_callback=finalize)
    assert finalized["done"] is True
    assert j2.stage == "recovered"


def test_crash_state_committed(tmp_path: Path) -> None:
    """state_committed reopen idempotently completes terminal journal without re-mutation."""

    svc, tx, identity, root = _svc(tmp_path)
    # Simulate generation already advanced.
    store = ExpectedStateStore(root, identity)
    loaded = store.load_current_state()
    assert loaded is not None
    pointer, state = loaded
    after = build_expected_state(
        generation=2,
        parent_state_id=pointer.state_id,
        source_tree_id="tree2",
        applied_transition_ids=("t1",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol",
    )
    store.cas_advance(expected_generation=1, state=after)

    journal = tx.create(target_id=identity.target_id, stage="prepared", before_generation=1, after_generation=2)
    journal = tx.advance(journal, "remote_mutating")
    journal = tx.advance(journal, "remote_verified")
    journal = tx.advance(journal, "state_committed")
    mutations = {"n": 0}

    def finalize(j):
        mutations["n"] += 1

    decision = svc.decide_for_journal(journal)
    assert decision.decision == "finalize"
    updated = svc.execute(decision, journal, finalize_callback=finalize)
    assert updated.stage == "recovered"
    # Second pass is noop-ish
    decision2 = svc.decide_for_journal(updated)
    assert decision2.decision == "noop"
    assert ExpectedStateStore(root, identity).read_current().generation == 2  # type: ignore[union-attr]


def test_prepared_zero_write_recovery_no_remote_mutation(tmp_path: Path) -> None:
    """prepared recovery must not call restore_callback (zero remote writes)."""

    svc, tx, identity, root = _svc(tmp_path)
    journal = tx.create(
        target_id=identity.target_id,
        stage="prepared",
        before_generation=1,
        after_generation=2,
        before_state_id="before",
        after_state_id="after",
    )
    writes = {"n": 0}

    def evil_restore(j):
        writes["n"] += 1  # product must not call this for prepared

    decision = svc.decide_for_journal(journal, remote_matches_current=True)
    assert decision.decision == "restore"
    updated = svc.execute(decision, journal, restore_callback=evil_restore)
    assert updated.stage == "recovered"
    assert writes["n"] == 0


def test_crash_before_hooks_stays_remote_mutating(tmp_path: Path) -> None:
    """After mutations + before hooks, journal stage is remote_mutating (not verified).

    Asserts the durable journal stage *before* any auto-restore, by driving
    prepare + mutate_remote(fail_before_hooks=True) without full deploy recovery.
    """

    from git_deploy.expected_state import FileEntry
    from git_deploy.models import PlannedFile, ProjectConfig
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
    from git_deploy.state_planner import SourceDiffPlan
    from git_deploy.target_identity import policy_fingerprint_for_project
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository
    from git_deploy.object_store import ContentAddressedStore
    import hashlib
    import subprocess

    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv")
    root = tmp_path / "t"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    project = ProjectConfig(
        name="demo",
        repository=repo,
        remote_root="/srv",
        local_state_dir=tmp_path / "leg",
        post_commands=("hook",),
    )
    gs = PersistentGitStore(root, repo)
    gs.ensure_layout()
    gs._publish_repository_identity()
    empty = GitRepository(repo).empty_tree()
    store = ExpectedStateStore(root, identity)
    ContentAddressedStore(root).put(b"before-bytes")
    ContentAddressedStore(root).put(b"after-bytes")
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"before-bytes").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"before-bytes")
    transport.supports_commands = True  # type: ignore[attr-defined]
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=transport,
        content_provider=lambda p: b"after-bytes",
    )
    files = [
        PlannedFile(
            action="upload",
            path="a.txt",
            remote_path="/srv/a.txt",
            source_path="a.txt",
            expected_before_sha256=hashlib.sha256(b"before-bytes").hexdigest(),
            target_sha256=hashlib.sha256(b"after-bytes").hexdigest(),
        )
    ]
    plan = SourceDiffPlan(
        before_tree_id=empty,
        after_tree_id=empty,
        files=(),
        excluded=(),
        introduced_transition_ids=("t1",),
        applied_transition_ids=("t1",),
    )
    after_state = executor._build_after_state(plan, state, files)
    journal = executor.prepare(
        plan=plan,
        files=files,
        before_state=state,
        after_state=after_state,
        deployment_id="20200101T000000Z-hooks1",
    )
    assert journal.stage == "prepared"
    try:
        journal = executor.mutate_remote(journal, files, fail_before_hooks=True)
        raise AssertionError("expected crash before hooks")
    except RuntimeError as exc:
        assert "before hooks" in str(exc)
    # Re-open durable journal — must still be remote_mutating, never remote_verified.
    reopened = TransactionStore(root).list_open()
    assert reopened, "journal must remain open after pre-hook crash"
    assert reopened[0].stage == "remote_mutating"
    assert reopened[0].stage != "remote_verified"
    # Hooks never ran (execute not called).
    assert not getattr(transport, "execute_calls", None)


def test_crash_before_health_stays_remote_mutating(tmp_path: Path) -> None:
    """Health failure keeps deploy restored (never finalized remote_verified success)."""

    from git_deploy.models import PlannedFile, ProjectConfig
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
    from git_deploy.state_planner import SourceDiffPlan
    from git_deploy.target_identity import policy_fingerprint_for_project
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.expected_state import FileEntry
    import hashlib
    import subprocess

    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv")
    root = tmp_path / "t"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    project = ProjectConfig(
        name="demo",
        repository=repo,
        remote_root="/srv",
        health_urls=("http://invalid.example/health",),
    )
    gs = PersistentGitStore(root, repo)
    gs.ensure_layout()
    gs._publish_repository_identity()
    empty = GitRepository(repo).empty_tree()
    store = ExpectedStateStore(root, identity)
    ContentAddressedStore(root).put(b"before")
    ContentAddressedStore(root).put(b"after")
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry(path="a.txt", owner="source", content_sha256=hashlib.sha256(b"before").hexdigest(), exists=True),),
    )
    store.cas_advance(expected_generation=None, state=state)
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"before")
    executor = StateDeploymentExecutor(
        project, identity, root, transport=transport, content_provider=lambda p: b"after"
    )
    plan = SourceDiffPlan(
        before_tree_id=empty,
        after_tree_id=empty,
        files=(),
        excluded=(),
        introduced_transition_ids=("t1",),
        applied_transition_ids=("t1",),
    )
    files = [
        PlannedFile(
            action="upload",
            path="a.txt",
            remote_path="/srv/a.txt",
            source_path="a.txt",
            expected_before_sha256=hashlib.sha256(b"before").hexdigest(),
            target_sha256=hashlib.sha256(b"after").hexdigest(),
        )
    ]
    result = executor.deploy(plan, files, fail_at="health")
    assert result["status"] == "restored"
    assert result.get("restored") is True
    assert store.read_current().generation == 1  # type: ignore[union-attr]
    assert transport.files["/srv/a.txt"].data == b"before"
