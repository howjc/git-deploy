"""Transactional deployment and rollback tests with an in-memory transport."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_deploy.errors import GitDeployError, RemoteDriftError
from git_deploy.executor import DeploymentExecutor
from git_deploy.models import DeploymentPlan, PlannedFile, ProjectConfig
from git_deploy.progress import ProgressEvent
from git_deploy.transport import ByteProgress


def _hash(data: bytes) -> str:
    """Return the SHA-256 digest used in test plans.

    Args:
        data: Test file bytes.

    Returns:
        Hexadecimal digest.
    """

    return hashlib.sha256(data).hexdigest()


class FakeTransport:
    """Minimal stateful transport used to assert remote side effects."""

    supports_commands = True

    def __init__(self, files: dict[str, bytes], fail_path: str | None = None):
        """Initialize remote files and an optional one-shot failure path.

        Args:
            files: Mutable remote path mapping.
            fail_path: Destination whose first replacement should fail.
        """

        self.files = files
        self.modes: dict[str, bool] = {}
        self.fail_path = fail_path
        self.write_count = 0
        self.closed = False

    def read_file(
        self,
        remote_path: str,
        progress: ByteProgress | None = None,
    ) -> bytes | None:
        """Return current test bytes or ``None``.

        Args:
            remote_path: Remote path.

        Returns:
            Stored bytes or ``None``.
        """

        data = self.files.get(remote_path)
        if data is not None and progress is not None:
            progress(len(data))
        return data

    def is_executable(self, remote_path: str) -> bool | None:
        """Return the stored executable bit when a file exists.

        Args:
            remote_path: Remote path.

        Returns:
            Executable state or ``None``.
        """

        return self.modes.get(remote_path, False) if remote_path in self.files else None

    def replace_file(
        self,
        remote_path: str,
        data: bytes,
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Replace bytes or trigger the configured one-shot failure.

        Args:
            remote_path: Remote destination.
            data: New bytes.
            executable: New executable state.
            progress: Optional uploaded-byte callback.
        """

        self.write_count += 1
        if self.fail_path == remote_path:
            self.fail_path = None
            raise GitDeployError(f"injected failure for {remote_path}")
        if progress is not None:
            progress(len(data))
        self.files[remote_path] = data
        self.modes[remote_path] = executable

    def delete_file(self, remote_path: str) -> None:
        """Delete one test path.

        Args:
            remote_path: Remote path.
        """

        self.write_count += 1
        self.files.pop(remote_path, None)
        self.modes.pop(remote_path, None)

    def execute(self, command: str) -> tuple[int, str, str]:
        """Treat configured test commands as successful.

        Args:
            command: Command text, unused.

        Returns:
            Successful process tuple.
        """

        del command
        return 0, "", ""

    def close(self) -> None:
        """Record that the executor released the transport."""

        self.closed = True


class StubPlanner:
    """Return preconfigured target bytes without a real Git repository."""

    def __init__(self, targets: dict[str, bytes]):
        """Store target bytes keyed by planned repository path.

        Args:
            targets: Upload content mapping.
        """

        self.targets = targets

    def target_bytes(self, plan: DeploymentPlan, operation: PlannedFile) -> bytes:
        """Return bytes for one upload operation.

        Args:
            plan: Owning plan, unused.
            operation: Upload operation.

        Returns:
            Configured target bytes.
        """

        del plan
        return self.targets[operation.path]


def _project(tmp_path: Path) -> ProjectConfig:
    """Build a project with isolated test state.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Test project configuration.
    """

    return ProjectConfig(
        name="demo",
        repository=tmp_path,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / "state",
    )


def _plan(tmp_path: Path) -> tuple[DeploymentPlan, StubPlanner]:
    """Build a plan containing modify, add, and delete operations.

    Args:
        tmp_path: Repository placeholder path.

    Returns:
        Test plan and matching target-byte planner.
    """

    old = b"old\n"
    removed = b"remove\n"
    new = b"new\n"
    added = b"added\n"
    files = (
        PlannedFile(
            action="upload",
            path="keep.txt",
            remote_path="/srv/demo/keep.txt",
            source_path="keep.txt",
            expected_before_sha256=_hash(old),
            target_sha256=_hash(new),
            target_size=len(new),
        ),
        PlannedFile(
            action="upload",
            path="added.txt",
            remote_path="/srv/demo/added.txt",
            source_path="added.txt",
            expected_before_sha256=None,
            target_sha256=_hash(added),
            target_size=len(added),
        ),
        PlannedFile(
            action="delete",
            path="remove.txt",
            remote_path="/srv/demo/remove.txt",
            source_path=None,
            expected_before_sha256=_hash(removed),
            target_sha256=None,
        ),
    )
    return (
        DeploymentPlan(
            project="demo",
            repository=tmp_path,
            remote_root="/srv/demo",
            from_commit="a" * 40,
            to_commit="b" * 40,
            files=files,
        ),
        StubPlanner({"keep.txt": new, "added.txt": added}),
    )


def test_deploy_and_rollback_restore_exact_remote_bytes(tmp_path: Path) -> None:
    """Persist backups, apply a plan, and reverse it by deployment ID."""

    remote = {
        "/srv/demo/keep.txt": b"old\n",
        "/srv/demo/remove.txt": b"remove\n",
    }
    transport = FakeTransport(remote)
    progress: list[ProgressEvent] = []
    executor = DeploymentExecutor(
        _project(tmp_path),
        {},
        transport_factory=lambda server: transport,
        health_checker=lambda url: None,
        progress_callback=progress.append,
    )
    plan, planner = _plan(tmp_path)

    manifest = executor.deploy(plan, planner)  # type: ignore[arg-type]

    assert manifest.status == "succeeded"
    assert remote == {
        "/srv/demo/keep.txt": b"new\n",
        "/srv/demo/added.txt": b"added\n",
    }
    loaded = executor.store.load(manifest.deployment_id)
    executor.rollback(loaded)
    assert remote == {
        "/srv/demo/keep.txt": b"old\n",
        "/srv/demo/remove.txt": b"remove\n",
    }
    assert executor.store.load(manifest.deployment_id).status == "rolled_back"
    assert any(event.phase == "check" and event.completed == 3 for event in progress)
    assert any(
        event.phase == "upload"
        and event.completed == 2
        and event.bytes_completed == len(b"new\n") + len(b"added\n")
        for event in progress
    )
    assert any(event.phase == "delete" and event.completed == 1 for event in progress)
    assert any(event.phase == "rollback" and event.completed == 3 for event in progress)


def test_remote_drift_blocks_deployment_without_writes(tmp_path: Path) -> None:
    """Reject a mismatched source baseline before creating backups or writing remotely."""

    remote = {
        "/srv/demo/keep.txt": b"operator change\n",
        "/srv/demo/remove.txt": b"remove\n",
    }
    transport = FakeTransport(remote)
    project = _project(tmp_path)
    executor = DeploymentExecutor(project, {}, transport_factory=lambda server: transport)
    plan, planner = _plan(tmp_path)

    with pytest.raises(RemoteDriftError, match="keep.txt"):
        executor.deploy(plan, planner)  # type: ignore[arg-type]

    assert transport.write_count == 0
    assert not (project.local_state_dir / "deployments").exists()  # type: ignore[operator]


def test_partial_failure_automatically_restores_remote_files(tmp_path: Path) -> None:
    """Restore every touched path when a later upload fails."""

    original = {
        "/srv/demo/keep.txt": b"old\n",
        "/srv/demo/remove.txt": b"remove\n",
    }
    remote = dict(original)
    transport = FakeTransport(remote, fail_path="/srv/demo/added.txt")
    executor = DeploymentExecutor(
        _project(tmp_path),
        {},
        transport_factory=lambda server: transport,
    )
    plan, planner = _plan(tmp_path)

    with pytest.raises(GitDeployError, match="remote files were restored"):
        executor.deploy(plan, planner)  # type: ignore[arg-type]

    assert remote == original
    manifests = executor.store.list_manifests()
    assert manifests[0].status == "auto_rolled_back"


def test_rollback_drift_is_read_only_and_keeps_manifest_succeeded(tmp_path: Path) -> None:
    """Do not rewrite remote files or history when rollback preflight detects drift."""

    remote = {
        "/srv/demo/keep.txt": b"old\n",
        "/srv/demo/remove.txt": b"remove\n",
    }
    transport = FakeTransport(remote)
    executor = DeploymentExecutor(
        _project(tmp_path),
        {},
        transport_factory=lambda server: transport,
    )
    plan, planner = _plan(tmp_path)
    manifest = executor.deploy(plan, planner)  # type: ignore[arg-type]
    remote["/srv/demo/keep.txt"] = b"operator change\n"
    writes_before = transport.write_count

    with pytest.raises(RemoteDriftError, match="changed after"):
        executor.rollback(executor.store.load(manifest.deployment_id))

    assert transport.write_count == writes_before
    assert executor.store.load(manifest.deployment_id).status == "succeeded"
