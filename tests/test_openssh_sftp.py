"""Native OpenSSH backend command, safety, cleanup, and pooling tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError
from git_deploy.transports import create_transport
from git_deploy.transports.openssh_sftp import (
    OpenSSHSFTPTransport,
    SSHConnectionPool,
    _find_posix_executable,
    _control_socket_root,
    _quote_sftp,
)
from git_deploy.transports.sftp import SFTPTransport


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
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("git_deploy.transports.openssh_sftp.shutil.which", which)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    target = native_target(tmp_path)
    transport = OpenSSHSFTPTransport(target)
    local = tmp_path / 'file "one".sh'
    local.write_text("#!/bin/sh\n", encoding="utf-8")

    transport.connect()
    directory = transport.master.directory  # type: ignore[union-attr]
    assert directory is not None
    assert os.stat(directory).st_mode & 0o777 == 0o700
    transport.ensure_root()
    transport.upload(local, "bin/file one.sh", lambda done, total: None, executable=True)
    transport.delete("bin/old.sh")
    transport.close()
    transport.close()

    masters = [command for command, _ in calls if "-MNf" in command]
    batches = [payload for command, payload in calls if command[0].endswith("/sftp")]
    assert len(masters) == 1
    assert all("BatchMode=yes" not in argument for command in masters for argument in command)
    assert any(payload and "chmod 0755" in payload for payload in batches)
    assert any(payload and "file one.sh" in payload for payload in batches)
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
        return subprocess.CompletedProcess(command, 0, "", "")

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
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    transport = OpenSSHSFTPTransport(native_target(tmp_path))
    local = tmp_path / "app.js"
    local.write_text("asset", encoding="utf-8")

    transport.connect()
    transport.upload(local, "app.js", lambda done, total: None)
    transport.close()

    assert any(".bak" in payload and payload.startswith("rename ") for payload in payloads)
    assert any(payload.startswith("-rm ") and ".bak" in payload for payload in payloads)
