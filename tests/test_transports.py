"""Protocol-specific safety behavior tests without real credentials."""

from __future__ import annotations

import errno
import ftplib
from pathlib import Path, PurePosixPath

import pytest
import subprocess

from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError
from git_deploy.transports.ftp import FTPTransport
from git_deploy.transports.sftp import SFTPTransport, _resolve_ssh_settings


class FakeSFTP:
    """Exercise temporary publish fallback ordering."""

    def __init__(self, *, target_exists: bool = True, publish_fails: bool = False) -> None:
        """Configure target presence and fallback publish outcome."""

        self.target_exists = target_exists
        self.publish_fails = publish_fails
        self.calls: list[tuple[str, str, str | None]] = []

    def posix_rename(self, source: str, target: str) -> None:
        """Simulate a server without the POSIX rename extension."""

        self.calls.append(("posix", source, target))
        raise OSError(errno.ENOSYS, "unsupported")

    def rename(self, source: str, target: str) -> None:
        """Rename target to backup, temp to target, or restore backup."""

        self.calls.append(("rename", source, target))
        if source == "/root/file" and not self.target_exists:
            raise OSError(errno.ENOENT, "missing")
        if source == "/root/temp" and self.publish_fails:
            raise OSError(errno.EIO, "failed")

    def remove(self, path: str) -> None:
        """Record backup cleanup."""

        self.calls.append(("remove", path, None))


class FakeFTP:
    """Provide configurable FTP delete replies."""

    def __init__(self, message: str) -> None:
        """Store the error reply returned by delete."""

        self.message = message

    def delete(self, path: str) -> None:
        """Raise the configured permanent FTP reply."""

        raise ftplib.error_perm(self.message)


class FakeSSHClient:
    """Record Paramiko connection options without opening a socket."""

    def __init__(self) -> None:
        """Initialize connection recording and a minimal SFTP closer."""

        self.connect_options: dict[str, object] = {}
        self.closed = False

    def load_system_host_keys(self) -> None:
        """Accept the system-host-key loading contract."""

    def load_host_keys(self, filename: str) -> None:
        """Accept an optional target-specific known-hosts path."""

    def set_missing_host_key_policy(self, policy: object) -> None:
        """Accept the configured strict or permissive policy."""

    def connect(self, **kwargs: object) -> None:
        """Record authentication, agent, and timeout settings."""

        self.connect_options = kwargs

    def open_sftp(self):  # noqa: ANN201
        """Return a closeable synthetic SFTP channel."""

        return self

    def close(self) -> None:
        """Record cleanup for either channel or client."""

        self.closed = True


def sftp_target() -> TargetConfig:
    """Return a compact SFTP target for unit adapters."""

    return TargetConfig("dev", "sftp", "host", "user", PurePosixPath("/root"), 22)


def ftp_target() -> TargetConfig:
    """Return a compact FTP target for unit adapters."""

    return TargetConfig(
        "prod",
        "ftp",
        "host",
        "user",
        PurePosixPath("/root"),
        21,
        password_env="PASSWORD",
    )


def test_sftp_fallback_preserves_target_before_publish() -> None:
    """Compatibility fallback renames the old target to backup before replacement."""

    transport = SFTPTransport(sftp_target())
    fake = FakeSFTP()
    transport.sftp = fake  # type: ignore[assignment]

    transport._publish_temporary("/root/temp", "/root/file")

    actions = [call[0] for call in fake.calls]
    assert actions[:3] == ["posix", "rename", "rename"]
    assert actions[-1] == "remove"


def test_sftp_failed_publish_attempts_backup_restore() -> None:
    """If replacement fails, fallback restores the old target instead of deleting it."""

    transport = SFTPTransport(sftp_target())
    fake = FakeSFTP(publish_fails=True)
    transport.sftp = fake  # type: ignore[assignment]

    with pytest.raises(DeployError, match="publish failed"):
        transport._publish_temporary("/root/temp", "/root/file")

    assert any(call[1].endswith(".bak") and call[2] == "/root/file" for call in fake.calls)


def test_ftp_missing_delete_is_idempotent() -> None:
    """An explicit no-such-file FTP reply is treated as an already-complete delete."""

    transport = FTPTransport(ftp_target())
    transport.ftp = FakeFTP("550 No such file")  # type: ignore[assignment]

    transport.delete("old.txt")


def test_ftp_permission_delete_still_fails() -> None:
    """FTP permission errors cannot be misclassified as an absent file."""

    transport = FTPTransport(ftp_target())
    transport.ftp = FakeFTP("550 Permission denied")  # type: ignore[assignment]

    with pytest.raises(DeployError, match="Permission denied"):
        transport.delete("old.txt")


def test_ssh_alias_uses_openssh_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSH alias resolution delegates Include/Match semantics to ``ssh -G``."""

    target = TargetConfig(
        "dev",
        "sftp",
        None,
        None,
        PurePosixPath("/root"),
        22,
        ssh_host_alias="project-dev",
    )

    def resolved(*args, **kwargs) -> subprocess.CompletedProcess[str]:  # noqa: ANN002, ANN003
        """Return representative OpenSSH effective configuration."""

        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout="hostname 192.0.2.10\nuser deploy\nport 2222\nidentityfile ~/.ssh/id_ed25519\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", resolved)

    settings = _resolve_ssh_settings(target)

    assert settings["host"] == "192.0.2.10"
    assert settings["username"] == "deploy"
    assert settings["port"] == 2222
    assert settings["key_files"] == [str(Path("~/.ssh/id_ed25519").expanduser())]


def test_sftp_connect_enables_only_explicit_agent_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paramiko receives SSH Agent and timeout settings without scanning ~/.ssh keys."""

    client = FakeSSHClient()
    monkeypatch.setattr("git_deploy.transports.sftp.paramiko.SSHClient", lambda: client)
    target = TargetConfig(
        "dev",
        "sftp",
        "host",
        "deploy",
        PurePosixPath("/root"),
        22,
        use_ssh_agent=True,
        strict_host_key_checking=True,
        timeout=7,
    )
    transport = SFTPTransport(target)

    transport.connect()

    assert client.connect_options["allow_agent"] is True
    assert client.connect_options["look_for_keys"] is False
    assert client.connect_options["timeout"] == 7
    transport.close()
    assert client.closed
