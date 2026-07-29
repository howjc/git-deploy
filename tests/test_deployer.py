"""Delayed state commit, exact bytes, retries, and convergence tests."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest

import git_deploy.deployer as deployer_module
from git_deploy.config import TargetConfig, load_config
from git_deploy.deployer import execute_plan, execute_recovery_plan
from git_deploy.errors import DeployError, PlanError
from git_deploy.ftp_hybrid import FTP_PENDING_SCHEMA, FTPHybridPending, FTPPendingPhase
from git_deploy.git import GitRepository
from git_deploy.manifest import ManifestEntry, StateStore, TargetState
from git_deploy.planner import FTPHybridFileUpload, FTPRecoveryPlan, create_plan
from git_deploy.progress import ProgressReporter
from git_deploy.transports.base import ProgressCallback, RemotePathType, Transport
from git_deploy.transports.base import TransferMeasurementMode
from git_deploy.transports.ftp import FTPTransport
from tests.conftest import commit_all, write_config


class FakeTransport(Transport):
    """Record remote mutations and optionally fail uploads."""

    def __init__(
        self,
        failures: int = 0,
        connect_failures: int = 0,
        command_failure: int | None = None,
    ) -> None:
        """Create a fake with requested upload and initial-connect failures."""

        self.failures = failures
        self.connect_failures = connect_failures
        self.connects = 0
        self.closed = 0
        self.files: dict[str, bytes] = {}
        self.deletes: list[str] = []
        self.commands: list[tuple[str, PurePosixPath, float | None]] = []
        self.command_failure = command_failure

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

    def run_command(
        self,
        command: str,
        *,
        cwd: PurePosixPath,
        timeout: float | None,
    ) -> None:
        """Record non-retried remote commands and fail one selected call."""

        assert self.closed == 0
        self.commands.append((command, cwd, timeout))
        if self.command_failure == len(self.commands):
            raise DeployError("remote command failed with exit=7")

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


class FailDeleteTransport(FakeTransport):
    """Fail every delete probe/operation and record retry invalidation."""

    def __init__(self) -> None:
        """Initialize delete attempts and invalidation evidence."""

        super().__init__()
        self.delete_attempts = 0
        self.invalidations = 0

    def delete(self, remote_path: str) -> None:
        """Simulate a permission/dead-session error instead of absence."""

        self.delete_attempts += 1
        raise OSError("Permission denied")

    def invalidate_connection(self) -> None:
        """Record that retries discard the failed connection."""

        self.invalidations += 1
        self.close()


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


def test_per_file_retry_does_not_rebuild_plan(
    git_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    output = capsys.readouterr().err
    assert output.count("TRANSFER SUMMARY") == 1
    assert "payload:        12 B" in output
    assert "attempt bytes:  12 B" in output
    assert "retries:        1" in output


def test_summary_stream_failure_after_state_commit_is_fail_open(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed Summary sink cannot fail deployment or trigger an upload retry."""

    class SummaryFailingStream:
        """Accept one completion line, then fail when Summary rendering begins."""

        def __init__(self) -> None:
            """Initialize the two writes used by one line-oriented completion."""

            self.write_calls = 0

        def isatty(self) -> bool:
            """Select non-TTY completion and Summary output."""

            return False

        def write(self, value: str) -> int:
            """Raise when the Summary heading attempts its first write."""

            self.write_calls += 1
            if self.write_calls > 2:
                raise OSError("summary sink closed")
            return len(value)

        def flush(self) -> None:
            """Accept completion-line flushing."""

    stream = SummaryFailingStream()

    def reporter_factory(verbose: bool = False, **kwargs):  # noqa: ANN003, ANN202
        """Inject the failing display stream into the real deployment flow."""

        return ProgressReporter(verbose, stream=stream, **kwargs)

    monkeypatch.setattr(deployer_module, "ProgressReporter", reporter_factory)
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    transport = FakeTransport()

    execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    assert store.load("dev") is not None
    assert transport.files["app.py"] == b"print('v1')\n"
    assert transport.connects == 1
    assert stream.write_calls == 3


def test_transport_measurement_mode_reaches_deployment_summary(
    git_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deployment construction binds a coarse backend to its shared Reporter."""

    class CoarseTransport(FakeTransport):
        """Model Native OpenSSH's callback and measurement capability."""

        measurement_mode = TransferMeasurementMode.COARSE

        def upload(
            self,
            local_path: Path,
            remote_path: str,
            callback: ProgressCallback,
            *,
            executable: bool = False,
        ) -> None:
            """Emit only Native-style start and completion callbacks."""

            del executable
            content = local_path.read_bytes()
            callback(0, len(content))
            self.files[remote_path] = content
            callback(len(content), len(content))

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    transport = CoarseTransport()

    execute_plan(plan, config, repository, store, transport_factory=lambda target: transport)

    output = capsys.readouterr().err
    assert "measurement:    coarse Native batch" in output
    assert "reported bytes: >= 12 B" in output
    assert "attempt bytes:" not in output
    assert "average upload:" not in output


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


def test_terminal_failure_keeps_old_state(
    git_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    assert "TRANSFER SUMMARY" not in capsys.readouterr().err


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


def test_after_deploy_runs_after_files_before_state_and_stops_on_failure(
    git_project: Path,
) -> None:
    """Command failure retains old State, stops the list, and reruns the whole plan."""

    config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
after_deploy = ["first", "second", "third"]
command_timeout = 9

[deploy]
retries = 3
retry_delay = 0
""",
        )
    )
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    target = config.target(None)
    old = TargetState(1, "dev", target.fingerprint, repository.head(), 1, {})
    store.save(old)
    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    commit_all(git_project, "command deployment change")
    plan = create_plan(config, target, repository, old, full=False)
    failed = FakeTransport(command_failure=2)

    with pytest.raises(DeployError, match="exit=7"):
        execute_plan(plan, config, repository, store, transport_factory=lambda target: failed)

    assert failed.files["app.py"] == b"print('v2')\n"
    assert [item[0] for item in failed.commands] == ["first", "second"]
    assert failed.commands[0][1:] == (PurePosixPath("/srv/app"), 9.0)
    assert failed.closed == 1
    assert store.load("dev") == old

    resumed = FakeTransport()
    execute_plan(plan, config, repository, store, transport_factory=lambda target: resumed)

    assert [item[0] for item in resumed.commands] == ["first", "second", "third"]
    assert resumed.files["app.py"] == b"print('v2')\n"
    assert store.load("dev") != old


def test_noop_does_not_connect_or_run_after_deploy(git_project: Path) -> None:
    """Command hooks do not turn an unchanged deployment into a service restart."""

    config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
after_deploy = ["restart-app"]
""",
        )
    )
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    first = create_plan(config, config.target(None), repository, None, full=False)
    execute_plan(first, config, repository, store, transport_factory=lambda target: FakeTransport())
    noop = create_plan(config, config.target(None), repository, store.load("dev"), full=False)
    transport = FakeTransport()

    execute_plan(noop, config, repository, store, transport_factory=lambda target: transport)

    assert transport.connects == 0
    assert transport.commands == []


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


def test_source_freeze_verifies_planned_sha_and_size_before_connect(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted Git export cannot enter the frozen upload set or reach transport."""

    aggregation = git_project / "dist"
    aggregation.mkdir()
    (aggregation / "index.html").write_text("hybrid", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
project_id = "github.com/acme/project"

[source]
include = ["**"]

[[outputs]]
name = "frontend-root"
local = "dist"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
        )
    )
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    plan = create_plan(config, config.target(None), repository, None, full=False)
    transport = FakeTransport()

    def corrupt_export(_commit: str, _path: str, destination: Path) -> None:
        """Write bytes that disagree with the plan's committed blob identity."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"corrupt")

    monkeypatch.setattr(repository, "export_file", corrupt_export)
    with pytest.raises(PlanError, match="source content does not match"):
        execute_plan(
            plan,
            config,
            repository,
            store,
            transport_factory=lambda target: transport,
        )
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


def test_delete_probe_error_keeps_state_and_delete_intent(git_project: Path) -> None:
    """An ambiguous delete failure cannot advance State or lose the next retry."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    store = StateStore(repository.git_dir())
    target = config.target(None)
    old = TargetState(
        1,
        "dev",
        target.fingerprint,
        repository.head(),
        1,
        {"public/dist/old.js": ManifestEntry("0" * 64, 1)},
    )
    store.save(old)
    plan = create_plan(config, target, repository, old, full=False)
    failed = FailDeleteTransport()

    with pytest.raises(DeployError, match="Permission denied"):
        execute_plan(plan, config, repository, store, transport_factory=lambda selected: failed)

    assert failed.delete_attempts == config.deploy.retries
    assert failed.invalidations == config.deploy.retries - 1
    assert store.load("dev") == old
    rerun = create_plan(config, target, repository, store.load("dev"), full=False)
    assert [operation.remote_path for operation in rerun.operations] == ["public/dist/old.js"]


def test_state_complete_ftp_recovery_restores_frozen_state_on_empty_clone(
    git_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-commit recovery always writes Pending State, even after phase advancement."""

    config = load_config(write_config(git_project))
    target = config.target(None)
    ftp_target = target.__class__(
        target.name,
        "ftp",
        "ftp.example.invalid",
        "deploy",
        target.remote_root,
        21,
        password_env="FTP_PASSWORD",
    )
    frozen_state = TargetState(1, "dev", ftp_target.fingerprint, "frozen-head", 9, {})
    pending = FTPHybridPending(
        FTP_PENDING_SCHEMA,
        "github.com/acme/project",
        "frontend-root",
        ".",
        ftp_target.fingerprint,
        "deployment-1",
        FTPPendingPhase.STATE_COMPLETE,
        "1" * 64,
        "2" * 64,
        "3" * 64,
        frozen_state.last_commit,
        frozen_state,
        10,
        "4" * 64,
        "5" * 64,
    )
    plan = FTPRecoveryPlan(
        ftp_target,
        ftp_target.fingerprint,
        pending.project_id,
        pending.mapping,
        pending.remote,
        pending.next_ownership_hash,
        pending,
    )
    store = StateStore(tmp_path)
    transport = FTPTransport(ftp_target)
    cleanup: list[FTPHybridPending] = []
    monkeypatch.setattr("git_deploy.deployer.validate_recovery_freshness", lambda *args: None)
    monkeypatch.setattr(
        "git_deploy.deployer._cleanup_ftp_hybrid_pending",
        lambda _transport, record: cleanup.append(record),
    )

    execute_recovery_plan(plan, store, transport, verbose=False)

    assert store.load("dev") == frozen_state
    assert cleanup == [pending]


def test_ftp_recovery_state_save_failure_does_not_cleanup_pending(
    git_project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local State write error leaves the remote Pending marker untouched."""

    config = load_config(write_config(git_project))
    base = config.target(None)
    target = base.__class__(
        base.name,
        "ftp",
        "ftp.example.invalid",
        "deploy",
        base.remote_root,
        21,
        password_env="FTP_PASSWORD",
    )
    state = TargetState(1, "dev", target.fingerprint, "frozen-head", 9, {})
    pending = FTPHybridPending(
        FTP_PENDING_SCHEMA,
        "github.com/acme/project",
        "frontend-root",
        ".",
        target.fingerprint,
        "deployment-1",
        FTPPendingPhase.STATE_COMPLETE,
        "1" * 64,
        "2" * 64,
        "3" * 64,
        state.last_commit,
        state,
        10,
        "4" * 64,
        "5" * 64,
    )
    plan = FTPRecoveryPlan(
        target,
        target.fingerprint,
        pending.project_id,
        pending.mapping,
        pending.remote,
        pending.next_ownership_hash,
        pending,
    )
    store = StateStore(tmp_path)
    transport = FTPTransport(target)
    cleaned = False
    monkeypatch.setattr("git_deploy.deployer.validate_recovery_freshness", lambda *args: None)

    def fail_save(_state: TargetState) -> None:
        """Simulate an atomic local State persistence failure."""

        raise OSError("disk full")

    def mark_cleanup(*_args: object) -> None:
        """Record any unsafe cleanup attempt after the failed State write."""

        nonlocal cleaned
        cleaned = True

    monkeypatch.setattr(store, "save", fail_save)
    monkeypatch.setattr("git_deploy.deployer._cleanup_ftp_hybrid_pending", mark_cleanup)

    with pytest.raises(OSError, match="disk full"):
        execute_recovery_plan(plan, store, transport, verbose=False)
    assert not cleaned


def test_ftp_hybrid_publish_skips_final_retr_after_stage_verified(tmp_path: Path) -> None:
    """Business publish trusts Stage SHA256 + rename; no final-path RETR."""

    target = TargetConfig(
        "dev",
        "ftp",
        "ftp.example.invalid",
        "deploy",
        PurePosixPath("/public_html"),
        21,
        password_env="FTP_PASSWORD",
    )
    payload = b"stage-verified-payload"
    local_path = tmp_path / "asset.js"
    local_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    operation = FTPHybridFileUpload(
        "assets/app.js",
        local_path,
        digest,
        len(payload),
    )
    stage_root = ".git-deploy/ftp-hybrid/stage/deployment-1"
    staged_path = f"{stage_root}/files/{operation.path}"

    class PublishFTP(FTPTransport):
        """Record RETR targets while modeling rename-replace consumption."""

        def __init__(self) -> None:
            """Seed one already stage-verified payload file."""

            super().__init__(target)
            self.files: dict[str, bytes] = {staged_path: payload}
            self.retr_paths: list[str] = []
            self.ftp = self  # type: ignore[assignment]

        def lstat(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> RemotePathType:
            """Classify synthetic Stage/final paths."""

            del allow_case_collisions
            if remote_path in self.files:
                return RemotePathType.FILE
            return RemotePathType.MISSING

        def read_file(
            self,
            remote_path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            """Record every RETR and return configured bytes."""

            del allow_case_collisions
            self.retr_paths.append(remote_path)
            data = self.files[remote_path]
            if len(data) > max_bytes:
                raise DeployError(f"exceeds {max_bytes}")
            return data

        def rename_replace(self, source: str, destination: str) -> None:
            """Move staged bytes to the final path like a replace rename."""

            self.files[destination] = self.files.pop(source)

        def upload(
            self,
            local_path: Path,
            remote_path: str,
            callback: ProgressCallback,
            *,
            executable: bool = False,
        ) -> None:
            """Unexpected restage in the happy path fails the test."""

            del local_path, remote_path, callback, executable
            raise AssertionError("publish happy path must not restage")

    transport = PublishFTP()
    progress = ProgressReporter(verbose=False)
    deployer_module._publish_ftp_hybrid_file(
        operation,
        local_path,
        stage_root,
        transport,
        progress,
        attempts=1,
        delay=0,
    )

    assert transport.files[operation.path] == payload
    assert staged_path not in transport.files
    assert transport.retr_paths == []


def test_ftp_hybrid_publish_restage_still_retr_verifies_stage(tmp_path: Path) -> None:
    """When Stage is missing, publish restages and RETR-verifies Stage only."""

    target = TargetConfig(
        "dev",
        "ftp",
        "ftp.example.invalid",
        "deploy",
        PurePosixPath("/public_html"),
        21,
        password_env="FTP_PASSWORD",
    )
    payload = b"restage-me"
    local_path = tmp_path / "chunk.js"
    local_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    operation = FTPHybridFileUpload("assets/chunk.js", local_path, digest, len(payload))
    stage_root = ".git-deploy/ftp-hybrid/stage/deployment-2"
    staged_path = f"{stage_root}/files/{operation.path}"

    class RestageFTP(FTPTransport):
        """Start with an empty Stage so publish must restage."""

        def __init__(self) -> None:
            """Create an empty remote file map."""

            super().__init__(target)
            self.files: dict[str, bytes] = {}
            self.retr_paths: list[str] = []
            self.ftp = self  # type: ignore[assignment]

        def lstat(
            self,
            remote_path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> RemotePathType:
            """Classify synthetic paths."""

            del allow_case_collisions
            if remote_path in self.files:
                return RemotePathType.FILE
            return RemotePathType.MISSING

        def read_file(
            self,
            remote_path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            """Record RETR of Stage bytes only."""

            del allow_case_collisions
            self.retr_paths.append(remote_path)
            data = self.files[remote_path]
            if len(data) > max_bytes:
                raise DeployError(f"exceeds {max_bytes}")
            return data

        def rename_replace(self, source: str, destination: str) -> None:
            """Consume the staged file into the final path."""

            self.files[destination] = self.files.pop(source)

        def upload(
            self,
            local_path: Path,
            remote_path: str,
            callback: ProgressCallback,
            *,
            executable: bool = False,
        ) -> None:
            """Store restaged payload and report transfer completion."""

            del executable
            data = local_path.read_bytes()
            self.files[remote_path] = data
            callback(len(data), len(data))

    transport = RestageFTP()
    progress = ProgressReporter(verbose=False)
    deployer_module._publish_ftp_hybrid_file(
        operation,
        local_path,
        stage_root,
        transport,
        progress,
        attempts=1,
        delay=0,
    )

    assert transport.files[operation.path] == payload
    assert transport.retr_paths == [staged_path]
    assert operation.path not in transport.retr_paths
