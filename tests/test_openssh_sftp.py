"""Native OpenSSH backend command, safety, cleanup, and pooling tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from git_deploy.config import TargetConfig
from git_deploy.deployer import _execute_with_retry
from git_deploy.errors import DeployError
from git_deploy.planner import UploadOperation
from git_deploy.progress import ProgressReporter
from git_deploy.transports import create_transport
from git_deploy.transports.openssh_sftp import (
    OpenSSHSFTPTransport,
    PathProbeResult,
    SSHConnectionPool,
    _classify_path_probe,
    _find_posix_executable,
    _control_socket_root,
    _quote_sftp,
)
from git_deploy.transports.sftp import SFTPTransport


def _success(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Return a successful process, including one stable ``ssh -G`` result."""

    output = "hostname 192.0.2.10\nuser deploy\nport 22\n" if "-G" in command else ""
    return subprocess.CompletedProcess(command, 0, output, "")


def native_target(tmp_path: Path, *, root: str = "/srv/app") -> TargetConfig:
    """Return a frozen alias target suitable for fake native process tests."""

    return TargetConfig(
        "prod",
        "sftp",
        "192.0.2.10",
        "deploy",
        PurePosixPath(root),
        22,
        ssh_host_alias="project-prod",
        ssh_resolved=True,
        runtime_dir=tmp_path / "state",
    )


def test_backend_selection_uses_alias_only_for_native(tmp_path: Path) -> None:
    """Alias targets select native OpenSSH while direct hosts retain Paramiko."""

    assert isinstance(create_transport(native_target(tmp_path)), OpenSSHSFTPTransport)
    direct = TargetConfig(
        "legacy",
        "sftp",
        "192.0.2.20",
        "deploy",
        PurePosixPath("/srv/app"),
        22,
        ssh_resolved=True,
    )
    assert isinstance(create_transport(direct), SFTPTransport)


def test_missing_or_windows_executables_fail_structurally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native backend never falls back to a Windows ``ssh.exe`` from WSL."""

    monkeypatch.setattr("git_deploy.transports.openssh_sftp.shutil.which", lambda name: None)
    with pytest.raises(DeployError, match="system 'ssh'"):
        _find_posix_executable("ssh")
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which",
        lambda name: "/mnt/c/Windows/System32/OpenSSH/ssh.exe",
    )
    with pytest.raises(DeployError, match="POSIX"):
        _find_posix_executable("ssh")


def test_batch_path_quoting_rejects_command_delimiters() -> None:
    """SFTP batch paths preserve spaces/quotes and reject newline injection."""

    assert _quote_sftp('a b"c') == '"a b\\"c"'
    with pytest.raises(DeployError, match="newline"):
        _quote_sftp("safe\nrm /important")


def test_long_common_dir_uses_short_private_socket_root(tmp_path: Path) -> None:
    """Deep worktree paths avoid sockaddr limits without sharing a public socket."""

    runtime = tmp_path / ("deep-" * 30) / ".git/git-deploy"
    root = _control_socket_root(runtime, "production")

    assert str(root).startswith(tempfile.gettempdir())
    assert f"git-deploy-{os.getuid()}" in str(root)


def test_master_once_batch_reuse_permissions_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One interactive master serves multiple batches and cleans its 0700 directory."""

    calls: list[tuple[list[str], str | None]] = []

    def which(name: str) -> str:
        """Return POSIX test executable paths."""

        return f"/usr/bin/{name}"

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Record master/control/batch calls and report success."""

        calls.append((list(command), kwargs.get("input")))
        return _success(list(command))

    monkeypatch.setattr("git_deploy.transports.openssh_sftp.shutil.which", which)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    target = native_target(tmp_path)
    transport = OpenSSHSFTPTransport(target)
    local = tmp_path / 'file "one".sh'
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    progress: list[tuple[int, int | None]] = []

    transport.connect()
    directory = transport.master.directory  # type: ignore[union-attr]
    assert directory is not None
    assert os.stat(directory).st_mode & 0o777 == 0o700
    transport.ensure_root()
    transport.upload(
        local,
        "bin/file one.sh",
        lambda done, total: progress.append((done, total)),
        executable=True,
    )
    transport.delete("bin/old.sh")
    transport.close()
    transport.close()

    masters = [command for command, _ in calls if "-MNf" in command]
    batches = [payload for command, payload in calls if command[0].endswith("/sftp")]
    assert len(masters) == 1
    assert all("BatchMode=yes" not in argument for command in masters for argument in command)
    assert any(payload and "chmod 0755" in payload for payload in batches)
    assert any(payload and "file one.sh" in payload for payload in batches)
    assert progress == [(0, local.stat().st_size), (local.stat().st_size, local.stat().st_size)]
    assert not directory.exists()


def test_connection_pool_reuses_endpoint_across_remote_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two repositories with one alias share a master until command-level cleanup."""

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Record process calls and report success."""

        calls.append(list(command))
        return _success(list(command))

    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    pool = SSHConnectionPool()
    first = OpenSSHSFTPTransport(native_target(tmp_path, root="/srv/api"), pool)
    second = OpenSSHSFTPTransport(native_target(tmp_path, root="/srv/web"), pool)

    first.connect()
    second.connect()
    assert first.master is second.master
    third = OpenSSHSFTPTransport(
        replace(native_target(tmp_path, root="/srv/admin"), ssh_host_alias="project-admin"),
        pool,
    )
    third.connect()
    assert third.master is not first.master
    first.close()
    second.close()
    third.close()
    assert len([command for command in calls if "-MNf" in command]) == 2
    pool.close_all()
    assert len([command for command in calls if "exit" in command]) == 2


def test_pool_sharing_is_independent_of_repository_timeout_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different connect policies share a healthy endpoint without batch deadlines."""

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Record subprocess policy while returning a stable endpoint."""

        calls.append(dict(kwargs))
        return _success(list(command))

    monkeypatch.setattr("git_deploy.config.subprocess.run", run)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    pool = SSHConnectionPool()
    first = OpenSSHSFTPTransport(replace(native_target(tmp_path), timeout=3), pool)
    second = OpenSSHSFTPTransport(
        replace(native_target(tmp_path, root="/srv/web"), timeout=120), pool
    )

    first.connect()
    second.connect()

    assert first.master is second.master
    assert all("timeout" not in kwargs for kwargs in calls)
    pool.close_all()


def test_native_publish_uses_backup_swap_when_direct_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing targets are backed up before a compatibility replacement."""

    payloads: list[str] = []
    rename_attempts = 0
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Fail only the first direct publish rename."""

        nonlocal rename_attempts
        payload = kwargs.get("input") or ""
        if payload:
            payloads.append(payload)
        if command[0].endswith("/sftp") and payload.startswith("rename "):
            rename_attempts += 1
            if rename_attempts == 1:
                return subprocess.CompletedProcess(command, 1, "", "overwrite unsupported")
        return _success(list(command))

    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    transport = OpenSSHSFTPTransport(native_target(tmp_path))
    local = tmp_path / "app.js"
    local.write_text("asset", encoding="utf-8")

    transport.connect()
    transport.upload(local, "app.js", lambda done, total: None)
    transport.close()

    assert any(".bak" in payload and payload.startswith("rename ") for payload in payloads)
    assert any(payload.startswith("-rm ") and ".bak" in payload for payload in payloads)


@pytest.mark.parametrize(
    "current",
    [
        "hostname 192.0.2.99\nuser deploy\nport 22\n",
        "hostname 192.0.2.10\nuser other\nport 22\n",
        "hostname 192.0.2.10\nuser deploy\nport 2222\n",
    ],
)
def test_alias_drift_after_prepare_fails_before_master_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: str,
) -> None:
    """Host, user, or port drift cannot redirect a confirmed deployment."""

    commands: list[list[str]] = []
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Return a changed current Alias without permitting ``-MNf``."""

        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, current if "-G" in command else "", "")

    monkeypatch.setattr("git_deploy.config.subprocess.run", run)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)

    with pytest.raises(DeployError, match="stale target: SSH alias changed"):
        OpenSSHSFTPTransport(native_target(tmp_path)).connect()

    assert not any("-MNf" in command for command in commands)


def test_native_commands_pin_endpoint_separate_connect_timeout_and_unbound_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approved endpoint fields are pinned while auth/upload have no Python deadline."""

    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Record command policy and return a stable Alias."""

        calls.append((list(command), dict(kwargs)))
        return _success(list(command))

    monkeypatch.setattr("git_deploy.config.subprocess.run", run)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    target = replace(native_target(tmp_path), timeout=7.2)
    transport = OpenSSHSFTPTransport(target)
    local = tmp_path / "large.bin"
    local.write_bytes(b"content")

    transport.connect()
    transport.upload(local, "large.bin", lambda done, total: None)
    transport.close()

    master_command, master_kwargs = next(
        (command, kwargs) for command, kwargs in calls if "-MNf" in command
    )
    batch_command, batch_kwargs = next(
        (command, kwargs) for command, kwargs in calls if command[0].endswith("/sftp")
    )
    for expected in (
        "HostName=192.0.2.10",
        "User=deploy",
        "Port=22",
        "ConnectTimeout=8",
    ):
        assert expected in master_command
        assert expected in batch_command
    assert "timeout" not in master_kwargs
    assert "timeout" not in batch_kwargs
    assert batch_kwargs["env"]["LC_ALL"] == "C"  # type: ignore[index]


@pytest.mark.parametrize(
    "detail",
    [
        "remote open('/srv/app/old.js'): Permission denied",
        "Connection closed",
        "Connection timed out",
        "ssh: connect to host proxy: Network is unreachable",
        "Couldn't stat remote file: Permission denied",
        "Control socket connect(/tmp/control.sock): No such file or directory",
    ],
)
def test_native_probe_never_treats_ambiguous_errors_as_missing(
    tmp_path: Path,
    detail: str,
) -> None:
    """Permission, dead-master, network, and timeout probes fail closed."""

    class FailedProbe:
        """Return one failed batch diagnostic."""

        def run_batch(self, commands, **kwargs):  # noqa: ANN001, ANN202
            """Return the configured non-missing diagnostic."""

            return subprocess.CompletedProcess([], 1, "", detail)

    transport = OpenSSHSFTPTransport(native_target(tmp_path))
    transport.master = FailedProbe()  # type: ignore[assignment]

    with pytest.raises(DeployError, match="inspect failed"):
        transport.delete("old.js")
    assert _classify_path_probe(1, detail, "/srv/app/old.js") is PathProbeResult.ERROR


@pytest.mark.parametrize(
    "detail",
    [
        "ls /srv/app/old.js: No such file or directory",
        "Couldn't stat remote file: No such file or directory",
        'Can\'t ls: "/srv/app/old.js" not found',
    ],
)
def test_native_probe_accepts_only_confirmed_missing(
    tmp_path: Path,
    detail: str,
) -> None:
    """C-locale no-such-file diagnostics preserve idempotent delete."""

    class MissingProbe:
        """Return a confirmed missing-path diagnostic."""

        def run_batch(self, commands, **kwargs):  # noqa: ANN001, ANN202
            """Return the configured no-such-file result."""

            return subprocess.CompletedProcess([], 1, "", detail)

    transport = OpenSSHSFTPTransport(native_target(tmp_path))
    transport.master = MissingProbe()  # type: ignore[assignment]

    assert transport._probe("/srv/app/old.js") is PathProbeResult.MISSING
    transport.delete("old.js")


def test_pool_evicts_dead_master_and_establishes_a_second_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached dead master is closed and replaced before reuse."""

    master_connections = 0
    health_checks = 0
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Make the first cached health check fail after initial connection."""

        nonlocal master_connections, health_checks
        command = list(command)
        commands.append(command)
        if "-G" in command:
            return _success(command)
        if "-MNf" in command:
            master_connections += 1
            return _success(command)
        if "check" in command:
            health_checks += 1
            return subprocess.CompletedProcess(command, 1 if health_checks == 2 else 0, "", "dead")
        return _success(command)

    monkeypatch.setattr("git_deploy.config.subprocess.run", run)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    pool = SSHConnectionPool()
    first = pool.acquire(native_target(tmp_path))
    replacement = pool.acquire(native_target(tmp_path, root="/srv/other"))

    assert replacement is not first
    assert master_connections == 2
    assert any("exit" in command for command in commands)
    assert len(pool._masters) == 1
    pool.close_all()


def test_pooled_transport_invalidation_evicts_failed_master(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operation retry reset removes the specific pooled connection."""

    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr("git_deploy.config.subprocess.run", lambda command, **kwargs: _success(list(command)))
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.subprocess.run",
        lambda command, **kwargs: _success(list(command)),
    )
    pool = SSHConnectionPool()
    transport = OpenSSHSFTPTransport(native_target(tmp_path), pool)
    transport.connect()
    failed = transport.master

    transport.invalidate_connection()

    assert failed is not None
    assert transport.master is None
    assert not pool._masters


def test_operation_retry_replaces_dead_pooled_master_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed batch retry establishes a second master instead of reusing death."""

    commands: list[list[str]] = []
    failed_once = False
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Fail the first upload batch while keeping probes/control operations stable."""

        nonlocal failed_once
        command = list(command)
        commands.append(command)
        if "-G" in command:
            return _success(command)
        payload = kwargs.get("input") or ""
        if command[0].endswith("/sftp") and payload.startswith("put ") and not failed_once:
            failed_once = True
            return subprocess.CompletedProcess(command, 255, "", "Connection closed")
        return _success(command)

    monkeypatch.setattr("git_deploy.config.subprocess.run", run)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    pool = SSHConnectionPool()
    transport = OpenSSHSFTPTransport(native_target(tmp_path), pool)
    transport.connect()
    local = tmp_path / "asset.js"
    local.write_text("asset", encoding="utf-8")
    operation = UploadOperation("asset.js", "output", local_path=local, size=5)

    _execute_with_retry(
        operation,
        {"asset.js": local},
        transport,
        ProgressReporter(),
        attempts=2,
        delay=0,
    )

    assert len([command for command in commands if "-MNf" in command]) == 2
    assert failed_once
    pool.close_all()
