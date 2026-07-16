"""Delayed state commit, exact bytes, retries, and convergence tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.config import load_config
from git_deploy.deployer import execute_plan
from git_deploy.errors import DeployError, PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore, TargetState
from git_deploy.planner import create_plan
from git_deploy.transports.base import ProgressCallback, Transport
from tests.conftest import commit_all, write_config


class FakeTransport(Transport):
    """Record remote mutations and optionally fail uploads."""

    def __init__(self, failures: int = 0, connect_failures: int = 0) -> None:
        """Create a fake with requested upload and initial-connect failures."""

        self.failures = failures
        self.connect_failures = connect_failures
        self.connects = 0
        self.closed = 0
        self.files: dict[str, bytes] = {}
        self.deletes: list[str] = []

    def connect(self) -> None:
        """Record one connection."""

        self.connects += 1
        if self.connect_failures:
            self.connect_failures -= 1
            raise OSError("temporary connection failure")

    def ensure_root(self) -> None:
        """Accept the synthetic remote root."""

    def root_exists(self) -> bool:
        """Report that the synthetic remote root is ready."""

        return True

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Read frozen bytes, failing first when configured."""

        if self.failures:
            self.failures -= 1
            raise OSError("temporary network failure")
        content = local_path.read_bytes()
        self.files[remote_path] = content
        callback(len(content), len(content))

    def delete(self, remote_path: str) -> None:
        """Record an idempotent delete."""

        self.files.pop(remote_path, None)
        self.deletes.append(remote_path)

    def close(self) -> None:
        """Record resource cleanup."""

        self.closed += 1


class FailOnPathTransport(FakeTransport):
    """Persist earlier files but fail all attempts for one selected path."""

    def __init__(self, failed_path: str) -> None:
        """Select the remote path that simulates a network interruption."""

        super().__init__()
        self.failed_path = failed_path

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Fail the selected path while retaining previously uploaded files."""

        if remote_path == self.failed_path:
            raise OSError("network interrupted")
        super().upload(local_path, remote_path, callback, executable=executable)


def test_success_uploads_exact_head_and_commits_state(git_project: Path) -> None:
    """Dirty worktree bytes are excluded and state appears only after remote success."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    (git_project / "app.py").write_text("print('dirty')\n", encoding="utf-8")
    transport = FakeTransport()

    execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert transport.files["app.py"] == b"print('v1')\n"
    assert store.load("dev").last_commit == repository.head()  # type: ignore[union-attr]
    assert transport.closed == 1


def test_per_file_retry_does_not_rebuild_plan(git_project: Path) -> None:
    """A transient file failure retries the frozen operation and then succeeds."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    transport = FakeTransport(failures=1)

    execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert transport.files["app.py"] == b"print('v1')\n"
    assert store.load("dev") is not None
    assert transport.connects == 2


def test_initial_connection_and_root_check_are_retried(git_project: Path) -> None:
    """Transient failure before the first upload uses the same retry policy."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    transport = FakeTransport(connect_failures=1)

    execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert transport.connects == 2
    assert transport.files["app.py"] == b"print('v1')\n"
    assert store.load("dev") is not None


def test_source_freeze_stays_bound_to_planned_commit_after_head_moves(
    git_project: Path,
) -> None:
    """A later commit cannot change bytes or State identity of an approved plan."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    planned_head = plan.head
    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    commit_all(git_project, "move HEAD after planning")
    transport = FakeTransport()

    execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert transport.files["app.py"] == b"print('v1')\n"
    state = store.load("dev")
    assert state is not None
    assert state.last_commit == planned_head


def test_terminal_failure_keeps_old_state(git_project: Path) -> None:
    """Exhausted retries leave local state untouched for rerun convergence."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    old = TargetState(1, "dev", config.target(None).fingerprint, repository.head(), 1, {})
    store.save(old)
    plan = create_plan(config, config.target(None), repository, None, full=True)
    transport = FakeTransport(failures=99)

    with pytest.raises(DeployError, match="failed after"):
        execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert store.load("dev") == old
    assert transport.closed == 2


def test_empty_plan_advances_commit_without_connecting(git_project: Path) -> None:
    """Excluded-only commit movement avoids a remote connection but prevents repeated diff work."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    store = StateStore(repository.git_dir())
    execute_plan(plan, config, repository, store, transport_factory=lambda target: FakeTransport())
    state = store.load("dev")
    assert state is not None
    no_change = create_plan(config, config.target(None), repository, state, full=False)
    transport = FakeTransport()

    execute_plan(no_change, config, repository, store, transport_factory=lambda target: transport)

    assert transport.connects == 0


def test_output_change_before_connect_fails_without_remote_or_state(git_project: Path) -> None:
    """Output bytes are hash-verified while freezing, before transport creation connects."""

    dist = git_project / "dist"
    dist.mkdir()
    asset = dist / "app.js"
    asset.write_text("planned", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    asset.write_text("changed", encoding="utf-8")
    transport = FakeTransport()

    with pytest.raises(PlanError, match="output changed"):
        execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert transport.connects == 0
    assert store.load("dev") is None


def test_partial_failure_then_rerun_converges(git_project: Path) -> None:
    """A remote with an earlier partial upload converges when the unchanged plan is rerun."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "asset.js").write_text("asset", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    interrupted = FailOnPathTransport("public/dist/asset.js")

    with pytest.raises(DeployError):
        execute_plan(plan, config, repository, store, transport_factory=lambda target: interrupted)
    assert interrupted.files["app.py"] == b"print('v1')\n"
    assert store.load("dev") is None

    resumed = FakeTransport()
    resumed.files.update(interrupted.files)
    execute_plan(plan, config, repository, store, transport_factory=lambda target: resumed)

    assert resumed.files == {
        "app.py": b"print('v1')\n",
        "public/dist/asset.js": b"asset",
    }
    assert store.load("dev") is not None
