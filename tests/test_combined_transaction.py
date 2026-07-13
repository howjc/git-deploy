"""Source/artifact unified transaction, recovery, and rollback tests."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from git_deploy.artifact_planner import ArtifactPlan
from git_deploy.combined_planner import CombinedPlanner
from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
from git_deploy.git_store import PersistentGitStore
from git_deploy.models import ArtifactConfig, PlannedFile, ProjectConfig
from git_deploy.object_store import ContentAddressedStore
from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
from git_deploy.state_planner import SourceDiffPlan
from git_deploy.state_rollback import StateRollbackService
from git_deploy.streaming import FileContentRef, StreamingContentProvider
from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity
from git_deploy.transaction import TransactionStore


def _git(repo: Path, *args: str) -> str:
    """Run Git in the transaction fixture repository."""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _upload(path: str, before: bytes | None, after: bytes, owner: str) -> tuple[PlannedFile, FileEntry]:
    """Return one upload operation and its target file entry."""

    before_hash = hashlib.sha256(before).hexdigest() if before is not None else None
    after_hash = hashlib.sha256(after).hexdigest()
    return (
        PlannedFile(
            "upload",
            path,
            f"/srv/{path}",
            path,
            before_hash,
            after_hash,
            len(after),
        ),
        FileEntry(path, owner, after_hash),
    )


@dataclass
class FailOnceTransport(InMemoryTransport):
    """Fail/corrupt one selected mutation, then allow automatic restore."""

    fail_path: str | None = None
    fail_kind: str = "write"
    fired: bool = False

    def write_file(self, remote_path: str, data: bytes, executable: bool = False) -> None:
        """Fail or corrupt the first matching upload only."""

        if remote_path == self.fail_path and not self.fired:
            self.fired = True
            if self.fail_kind == "corrupt":
                return super().write_file(remote_path, b"corrupt", executable)
            raise OSError("injected upload failure")
        super().write_file(remote_path, data, executable)

    def write_file_stream(self, remote_path: str, chunks, executable: bool = False) -> None:
        """Consume chunks and delegate to the fail-once bytes writer."""

        self.write_file(remote_path, b"".join(chunks), executable)

    def delete_file(self, remote_path: str) -> None:
        """Fail the first matching delete only."""

        if remote_path == self.fail_path and self.fail_kind == "delete" and not self.fired:
            self.fired = True
            raise OSError("injected delete failure")
        super().delete_file(remote_path)


def _fixture(tmp_path: Path, transport: InMemoryTransport | None = None):
    """Seed one current source+artifact state and return a combined deploy fixture."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "T")
    (repo / "source.txt").write_bytes(b"old-source")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-qm", "old")
    before_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "source.txt").write_bytes(b"new-source")
    _git(repo, "add", "source.txt")
    _git(repo, "commit", "-qm", "new")
    after_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    project = ProjectConfig(
        name="demo",
        repository=repo,
        remote_root="/srv",
        local_state_dir=tmp_path / "state",
        artifacts=(ArtifactConfig("dist", "public", "tree"),),
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    git_store = PersistentGitStore(root, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    before_bytes = {
        "source.txt": b"old-source",
        "public/app.js": b"old-artifact",
        "public/remove.js": b"remove-me",
    }
    cas = ContentAddressedStore(root)
    for data in before_bytes.values():
        cas.put(data)
    before_entries = tuple(
        FileEntry(
            path,
            "source" if path == "source.txt" else "artifact:public",
            hashlib.sha256(data).hexdigest(),
        )
        for path, data in before_bytes.items()
    )
    before_state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=before_tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=before_entries,
        artifacts=({"mode": "known_source", "build_fingerprint": "old"},),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=before_state)

    remote = transport or InMemoryTransport()
    for path, data in before_bytes.items():
        remote.files[f"/srv/{path}"] = FakeRemotePath(data)
    source_upload, source_entry = _upload("source.txt", b"old-source", b"new-source", "source")
    artifact_upload, artifact_entry = _upload(
        "public/app.js", b"old-artifact", b"new-artifact", "artifact:public"
    )
    artifact_add, artifact_add_entry = _upload(
        "public/add.js", None, b"added-artifact", "artifact:public"
    )
    artifact_delete = PlannedFile(
        "delete",
        "public/remove.js",
        "/srv/public/remove.js",
        None,
        hashlib.sha256(b"remove-me").hexdigest(),
        None,
    )
    source_plan = SourceDiffPlan(
        before_tree,
        after_tree,
        (source_upload,),
        (),
        ("t1",),
        ("t0", "t1"),
    )
    artifact_plan = ArtifactPlan(
        "ready",
        (artifact_upload, artifact_add, artifact_delete),
        (artifact_entry, artifact_add_entry),
        (
            ("public/app.js", artifact_entry.content_sha256 or ""),
            ("public/add.js", artifact_add_entry.content_sha256 or ""),
        ),
    )
    combined = CombinedPlanner().combine(before_entries, source_plan, artifact_plan)
    content_root = tmp_path / "content"
    refs: dict[str, FileContentRef] = {}
    target_bytes = {
        "source.txt": b"new-source",
        "public/app.js": b"new-artifact",
        "public/add.js": b"added-artifact",
    }
    for path, data in target_bytes.items():
        local = content_root / path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        refs[path] = FileContentRef(local, hashlib.sha256(data).hexdigest(), len(data))
    provider = StreamingContentProvider(refs, tmp_path / "spool", chunk_size=3)
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=remote,
        content_provider=provider,
    )
    provenance = ({"mode": "known_source", "build_fingerprint": "new"},)
    return executor, remote, store, identity, root, source_plan, combined, provenance, before_bytes


def test_combined_transaction_success_and_latest_rollback(tmp_path: Path) -> None:
    """Source/artifacts commit one state and latest rollback restores the full before state."""

    executor, remote, store, identity, root, source, combined, provenance, before = _fixture(tmp_path)
    result = executor.deploy(
        source,
        combined.files,
        target_entries=combined.target_entries,
        artifact_provenance=provenance,
    )
    assert result["status"] == "succeeded"
    loaded = store.load_current_state()
    assert loaded is not None
    _pointer, state = loaded
    assert state.generation == 2
    assert state.artifacts[0]["build_fingerprint"] == "new"
    assert {entry.owner for entry in state.files} == {"source", "artifact:public"}
    assert TransactionStore(root).list_open() == []

    rolled = StateRollbackService(
        executor.project, identity, root, transport=remote
    ).rollback_latest()
    assert rolled.status == "succeeded"
    for path, data in before.items():
        assert remote.files[f"/srv/{path}"].data == data
    assert "/srv/public/add.js" not in remote.files
    current = store.load_current_state()
    assert current is not None
    assert current[1].generation == 3
    assert current[1].artifacts[0]["build_fingerprint"] == "old"


@pytest.mark.parametrize(
    ("fail_path", "fail_kind", "fail_at"),
    [
        ("/srv/source.txt", "write", None),
        ("/srv/public/app.js", "write", None),
        ("/srv/public/remove.js", "delete", None),
        ("/srv/public/app.js", "corrupt", None),
        (None, "write", "hook"),
        (None, "write", "health"),
    ],
)
def test_combined_transaction_failure_restores_unified_before(
    tmp_path: Path,
    fail_path: str | None,
    fail_kind: str,
    fail_at: str | None,
) -> None:
    """Every source/artifact mutation or post-check failure restores bytes and state."""

    transport = FailOnceTransport(fail_path=fail_path, fail_kind=fail_kind)
    executor, remote, store, _identity, root, source, combined, provenance, before = _fixture(
        tmp_path, transport
    )
    result = executor.deploy(
        source,
        combined.files,
        fail_at=fail_at,
        target_entries=combined.target_entries,
        artifact_provenance=provenance,
    )
    assert result["status"] == "restored"
    for path, data in before.items():
        assert remote.files[f"/srv/{path}"].data == data
    assert "/srv/public/add.js" not in remote.files
    current = store.load_current_state()
    assert current is not None and current[1].generation == 1
    assert current[1].artifacts[0]["build_fingerprint"] == "old"
    assert TransactionStore(root).list_open() == []
