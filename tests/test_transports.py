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
from git_deploy.transports.base import RemotePathType
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


class FakeCommandChannel:
    """Provide pollable Paramiko stdout, stderr, timeout, and exit status."""

    def __init__(
        self,
        *,
        stdout: tuple[bytes, ...] = (),
        stderr: tuple[bytes, ...] = (),
        status: int = 0,
        finishes: bool = True,
    ) -> None:
        """Configure output chunks and terminal status behavior."""

        self.stdout = list(stdout)
        self.stderr = list(stderr)
        self.status = status
        self.finishes = finishes
        self.closed = False

    def recv_ready(self) -> bool:
        """Return whether stdout has a buffered chunk."""

        return bool(self.stdout)

    def recv(self, size: int) -> bytes:
        """Return the next stdout chunk."""

        return self.stdout.pop(0)

    def recv_stderr_ready(self) -> bool:
        """Return whether stderr has a buffered chunk."""

        return bool(self.stderr)

    def recv_stderr(self, size: int) -> bytes:
        """Return the next stderr chunk."""

        return self.stderr.pop(0)

    def exit_status_ready(self) -> bool:
        """Return whether the synthetic process has exited."""

        return self.finishes

    def recv_exit_status(self) -> int:
        """Return the configured remote exit status."""

        return self.status

    def close(self) -> None:
        """Record timeout-driven channel cleanup."""

        self.closed = True


class FakeCommandFile:
    """Represent one closeable Paramiko command stream."""

    def __init__(self, channel: FakeCommandChannel) -> None:
        """Bind the shared exec channel."""

        self.channel = channel
        self.closed = False

    def close(self) -> None:
        """Record stream cleanup."""

        self.closed = True


class FakeCommandClient:
    """Record Paramiko exec options and expose one configured channel."""

    def __init__(self, channel: FakeCommandChannel) -> None:
        """Create stdin/stdout/stderr stream objects over one channel."""

        self.channel = channel
        self.stdin = FakeCommandFile(channel)
        self.stdout = FakeCommandFile(channel)
        self.stderr = FakeCommandFile(channel)
        self.calls: list[tuple[str, float | None, bool]] = []

    def exec_command(self, command: str, *, timeout: float | None, get_pty: bool):
        """Return streams while recording non-interactive execution policy."""

        self.calls.append((command, timeout, get_pty))
        return self.stdin, self.stdout, self.stderr


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


@pytest.mark.parametrize("unsafe_name", [" leading", "trailing ", "tab\tname"])
def test_paramiko_listing_rejects_unstable_remote_names(unsafe_name: str) -> None:
    """Paramiko listings use the same fail-closed component boundary as Native."""

    class ListingSFTP:
        """Expose one unsafe name through Paramiko's listdir API."""

        def listdir(self, path: str) -> list[str]:
            """Return the configured unsafe direct child."""

            return [unsafe_name]

    transport = SFTPTransport(sftp_target())
    transport.sftp = ListingSFTP()  # type: ignore[assignment]
    transport.lstat = lambda remote_path: RemotePathType.DIRECTORY  # type: ignore[method-assign]

    with pytest.raises(DeployError, match="unsafe name"):
        transport.list_directory("assets")


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


def test_ftp_missing_parent_probe_is_cached_and_root_probe_is_three_state() -> None:
    """Repeated missing-parent deletes avoid network probes and Doctor fails closed."""

    fake = CachingFTP({"/": {"root"}, "/root": set()})
    transport = FTPTransport(ftp_target())
    transport.ftp = fake  # type: ignore[assignment]

    transport.delete("gone/one.js")
    transport.delete("gone/two.js")

    assert fake.nlst_calls == ["/root/gone", "/root"]
    assert transport._missing_directories == {"/root/gone"}
    assert transport.root_exists()

    class DeniedFTP(CachingFTP):
        """Reject root inspection explicitly."""

        def nlst(self, path: str) -> list[str]:
            """Return one access failure."""

            raise ftplib.error_perm("550 Permission denied")

    denied = FTPTransport(ftp_target())
    denied.ftp = DeniedFTP({"/root": set()})  # type: ignore[assignment]
    with pytest.raises(DeployError, match="Permission denied"):
        denied.root_exists()


def test_ftp_created_directory_invalidates_missing_cache() -> None:
    """Creating a formerly missing directory makes later probes authoritative."""

    fake = CachingFTP({"/": {"root"}, "/root": set()})
    transport = FTPTransport(ftp_target())
    transport.ftp = fake  # type: ignore[assignment]
    transport._missing_directories.add("/root/new")

    transport._mkdirs("/root/new")

    assert "/root/new" not in transport._missing_directories
    assert transport._directory_entries["/root/new"] == set()


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


def test_paramiko_command_uses_cwd_no_pty_timeout_and_streams_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Direct-host SFTP reuses SSHClient and drains stdout/stderr before exit."""

    channel = FakeCommandChannel(stdout=(b"ready\n",), stderr=(b"warning\n",))
    client = FakeCommandClient(channel)
    transport = SFTPTransport(sftp_target())
    transport.client = client  # type: ignore[assignment]

    transport.run_command(
        "printf ready",
        cwd=PurePosixPath("/root/app with spaces"),
        timeout=12,
    )

    captured = capfd.readouterr()
    assert captured.out == "ready\n"
    assert captured.err == "warning\n"
    assert client.calls == [("cd -- '/root/app with spaces' && printf ready", 12, False)]
    assert client.stdin.closed
    assert client.stdout.closed
    assert client.stderr.closed
    assert channel.closed


def test_paramiko_command_nonzero_and_timeout_fail_closed() -> None:
    """Remote exit failure and whole-command timeout both raise without retry."""

    failed_client = FakeCommandClient(FakeCommandChannel(status=17))
    failed = SFTPTransport(sftp_target())
    failed.client = failed_client  # type: ignore[assignment]

    with pytest.raises(DeployError, match="exit=17"):
        failed.run_command("false", cwd=PurePosixPath("/root"), timeout=5)

    timeout_channel = FakeCommandChannel(finishes=False)
    timeout_client = FakeCommandClient(timeout_channel)
    timed = SFTPTransport(sftp_target())
    timed.client = timeout_client  # type: ignore[assignment]

    with pytest.raises(DeployError, match="timed out"):
        timed.run_command("sleep 10", cwd=PurePosixPath("/root"), timeout=0.001)
    assert timeout_channel.closed
    assert timeout_client.stdin.closed
    assert timeout_client.stdout.closed
    assert timeout_client.stderr.closed
