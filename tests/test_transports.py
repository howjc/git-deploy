"""Protocol-specific safety behavior tests without real credentials."""

from __future__ import annotations

import ftplib
import errno
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

    def chmod(self, path: str, mode: int) -> None:
        """Record the mode applied before publication."""

        self.calls.append(("chmod", path, str(mode)))


class FakeFTP:
    """Provide configurable FTP delete replies."""

    def __init__(self, message: str) -> None:
        """Store the error reply returned by delete."""

        self.message = message

    def delete(self, path: str) -> None:
        """Raise the configured permanent FTP reply."""

        raise ftplib.error_perm(self.message)

    def nlst(self, path: str) -> list[str]:
        """List the target only for the permission-error fixture."""

        return [f"{path}/old.txt"] if "Permission" in self.message else []


class CachingFTP:
    """Model directory listings, empty/missing parents, uploads, and deletes."""

    def __init__(self, entries: dict[str, set[str]]) -> None:
        """Initialize mutable absolute directory entries and call counters."""

        self.entries = entries
        self.nlst_calls: list[str] = []
        self.deleted: list[str] = []
        self.current = "/"

    def nlst(self, path: str) -> list[str]:
        """Return one directory listing or a permanent missing-parent response."""

        self.nlst_calls.append(path)
        if path not in self.entries:
            raise ftplib.error_perm("550 No such directory")
        return [f"{path.rstrip('/')}/{name}" for name in sorted(self.entries[path])]

    def pwd(self) -> str:
        """Return the synthetic current directory."""

        return self.current

    def cwd(self, path: str) -> None:
        """Enter existing directories and reject missing ones."""

        if path not in self.entries and path != "/":
            raise ftplib.error_perm("550 No such directory")
        self.current = path

    def mkd(self, path: str) -> None:
        """Create a directory or report that it already exists."""

        if path in self.entries:
            raise ftplib.error_perm("550 Already exists")
        self.entries[path] = set()

    def storbinary(self, command, handle, *, blocksize, callback):  # noqa: ANN001, ANN201
        """Store all bytes and expose the uploaded basename in the fake server."""

        payload = handle.read()
        callback(payload)
        target = command.removeprefix("STOR ")
        parent = PurePosixPath(target).parent.as_posix()
        self.entries[parent].add(PurePosixPath(target).name)

    def delete(self, path: str) -> None:
        """Delete an existing child and record the mutation."""

        parent = PurePosixPath(path).parent.as_posix()
        name = PurePosixPath(path).name
        self.entries[parent].remove(name)
        self.deleted.append(path)

    def quit(self) -> None:
        """Accept clean transport shutdown."""


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


def test_ftp_bulk_delete_lists_one_parent_once() -> None:
    """Deleting many hashed assets reuses one complete parent listing."""

    files = {f"asset-{index}.js" for index in range(120)}
    fake = CachingFTP({"/root": {"assets"}, "/root/assets": set(files)})
    transport = FTPTransport(ftp_target())
    transport.ftp = fake  # type: ignore[assignment]

    for name in sorted(files):
        transport.delete(f"assets/{name}")

    assert fake.nlst_calls == ["/root/assets"]
    assert len(fake.deleted) == 120


def test_ftp_missing_parent_and_empty_parent_are_idempotent() -> None:
    """A removed parent and a server returning 550 for an empty parent both converge."""

    fake = CachingFTP({"/": {"root"}, "/root": {"empty"}, "/root/empty": set()})
    transport = FTPTransport(ftp_target())
    transport.ftp = fake  # type: ignore[assignment]

    transport.delete("missing/old.txt")

    original_nlst = fake.nlst

    def empty_parent_550(path: str) -> list[str]:
        """Reproduce FTP servers that reject NLST for an empty directory."""

        if path == "/root/empty":
            fake.nlst_calls.append(path)
            raise ftplib.error_perm("550 No files found")
        return original_nlst(path)

    fake.nlst = empty_parent_550  # type: ignore[method-assign]
    transport.delete("empty/old.txt")

    assert fake.deleted == []


def test_ftp_listing_permission_error_fails_closed() -> None:
    """An explicit listing access failure never advances an idempotent delete."""

    class DeniedFTP(CachingFTP):
        """Reject the target parent listing with an access error."""

        def nlst(self, path: str) -> list[str]:
            """Return a permanent permission failure."""

            raise ftplib.error_perm("550 Permission denied")

    transport = FTPTransport(ftp_target())
    transport.ftp = DeniedFTP({"/root": {"old.txt"}})  # type: ignore[assignment]

    with pytest.raises(DeployError, match="Permission denied"):
        transport.delete("old.txt")


def test_ftp_upload_and_delete_keep_cached_listing_coherent(tmp_path: Path) -> None:
    """Successful uploads join and deletes leave the connection-scoped cache."""

    fake = CachingFTP({"/": {"root"}, "/root": {"old.txt"}})
    transport = FTPTransport(ftp_target())
    transport.ftp = fake  # type: ignore[assignment]
    payload = tmp_path / "new.txt"
    payload.write_text("new", encoding="utf-8")

    transport.delete("absent.txt")
    transport.upload(payload, "new.txt", lambda done, total: None)
    transport.delete("new.txt")
    transport.delete("new.txt")

    assert fake.nlst_calls == ["/root"]
    assert fake.deleted == ["/root/new.txt"]
    transport.close()
    assert transport._directory_entries == {}


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
    assert settings["key_files"] == []
    assert target.fingerprint == "sftp:deploy@192.0.2.10:2222:/root"


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
