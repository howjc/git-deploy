"""Protocol-specific safety behavior tests without real credentials."""

from __future__ import annotations

import ftplib
import errno
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
import subprocess

from git_deploy.config import TargetConfig
from git_deploy.deployer import _execute_with_retry, _retry_ftp_mutation
from git_deploy.errors import DeployError
from git_deploy.planner import DeleteOperation, UploadOperation
from git_deploy.progress import ProgressReporter
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


class ConnectableFTPSession:
    """Record login, UTF-8 negotiation, and cleanup across reconnects."""

    def __init__(
        self,
        *,
        banner: str = "220 stable",
        opts_error: bool = False,
        opts_error_reply: str = "504 Unknown command",
        feat_utf8: bool = True,
        opts_temp_error: bool = False,
    ) -> None:
        """Configure server identity and optional OPTS rejection.

        Args:
            banner: Welcome banner used for reconnect identity checks.
            opts_error: When True, OPTS raises a permanent (5xx) error_perm.
            opts_error_reply: Reply text for the permanent OPTS rejection.
            feat_utf8: Whether FEAT advertises the UTF8 feature token.
            opts_temp_error: When True, OPTS raises a temporary/network error.
        """

        self.banner = banner
        self.opts_error = opts_error
        self.opts_error_reply = opts_error_reply
        self.feat_utf8 = feat_utf8
        self.opts_temp_error = opts_temp_error
        self.commands: list[str] = []
        self.passive: bool | None = None
        self.encoding = "latin-1"
        self.closed = False

    def connect(self, host: str, port: int, *, timeout: float) -> None:
        """Accept the configured endpoint without opening a socket."""

        del host, port, timeout

    def login(self, username: str, password: str) -> None:
        """Accept synthetic credentials."""

        del username, password

    def set_pasv(self, passive: bool) -> None:
        """Record active/passive mode selection."""

        self.passive = passive

    def getwelcome(self) -> str:
        """Return the configured reconnect identity."""

        return self.banner

    def sendcmd(self, command: str) -> str:
        """Advertise UTF-8 and optionally reject its per-session activation."""

        self.commands.append(command)
        if command == "FEAT":
            if self.feat_utf8:
                return "211-Features\n UTF8\n MLSD\n211 End"
            return "211-Features\n MLSD\n211 End"
        if self.opts_temp_error:
            raise ftplib.error_temp("421 Service not available")
        if self.opts_error:
            raise ftplib.error_perm(self.opts_error_reply)
        return "200 UTF8 enabled"

    def quit(self) -> None:
        """Record normal session cleanup."""

        self.closed = True

    def close(self) -> None:
        """Record forced session cleanup."""

        self.closed = True


class RetryingUnicodeFTPTransport(FTPTransport):
    """Fail one Unicode business operation, then require a UTF-8 reconnect."""

    def __init__(self, target: TargetConfig) -> None:
        """Initialize per-operation failure and success counters."""

        super().__init__(target)
        self.upload_attempts = 0
        self.delete_attempts = 0

    def ensure_root(self) -> None:
        """Accept the synthetic root during the retry reconnect helper."""

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback,  # noqa: ANN001
        *,
        executable: bool = False,
    ) -> None:
        """Fail once and then accept a Unicode upload only under UTF-8."""

        del executable
        assert "部署" in remote_path
        assert self._require_ftp().encoding == "utf-8"
        self.upload_attempts += 1
        if self.upload_attempts == 1:
            raise OSError("transient upload failure")
        size = local_path.stat().st_size
        callback(size, size)

    def delete(self, remote_path: str) -> None:
        """Fail once and then accept a Unicode delete only under UTF-8."""

        assert "部署" in remote_path
        assert self._require_ftp().encoding == "utf-8"
        self.delete_attempts += 1
        if self.delete_attempts == 1:
            raise OSError("transient delete failure")


@pytest.mark.parametrize("passive", (True, False))
def test_ftp_utf8_requirement_is_restored_on_every_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    passive: bool,
) -> None:
    """Sticky UTF-8 sends FEAT and OPTS on new passive and active sessions."""

    sessions = [ConnectableFTPSession(), ConnectableFTPSession()]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = FTPTransport(replace(ftp_target(), passive=passive))

    transport.connect()
    first = transport.ftp
    assert isinstance(first, ConnectableFTPSession)
    transport.enable_utf8()
    transport.close()
    transport.connect()
    second = transport.ftp

    assert isinstance(second, ConnectableFTPSession)
    assert first.commands == ["FEAT", "OPTS UTF8 ON"]
    assert second.commands == ["FEAT", "OPTS UTF8 ON"]
    assert second.encoding == "utf-8"
    assert second.passive is passive


def test_ftp_pureftpd_opts_rejection_still_enables_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure-FTPd style OPTS 504 still activates client UTF-8 when FEAT has UTF8."""

    sessions = [
        ConnectableFTPSession(opts_error=True, opts_error_reply="504 Unknown command"),
        ConnectableFTPSession(opts_error=True, opts_error_reply="504 Unknown command"),
    ]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = FTPTransport(ftp_target())

    transport.connect()
    transport.enable_utf8()
    first = transport.ftp
    assert isinstance(first, ConnectableFTPSession)
    assert first.encoding == "utf-8"
    transport.close()
    transport.connect()
    second = transport.ftp

    assert isinstance(second, ConnectableFTPSession)
    assert first.commands == ["FEAT", "OPTS UTF8 ON"]
    assert second.commands == ["FEAT", "OPTS UTF8 ON"]
    assert second.encoding == "utf-8"


@pytest.mark.parametrize("reply", ("500 Syntax error", "501 Bad args", "502 Not implemented"))
def test_ftp_opts_unsupported_codes_enable_always_on_utf8(
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
) -> None:
    """Only 500/501/502/504 permanent OPTS replies are treated as always-on UTF-8."""

    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(
        ftplib,
        "FTP",
        lambda: ConnectableFTPSession(opts_error=True, opts_error_reply=reply),
    )
    transport = FTPTransport(ftp_target())
    transport.connect()
    transport.enable_utf8()
    assert isinstance(transport.ftp, ConnectableFTPSession)
    assert transport.ftp.encoding == "utf-8"


@pytest.mark.parametrize(
    "reply",
    (
        "530 Not logged in",
        "532 Need account for login",
        "550 Permission denied",
        "553 Filename not allowed",
    ),
)
def test_ftp_opts_other_permanent_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
) -> None:
    """Auth/permission/policy 5xx on OPTS must not be treated as always-on UTF-8."""

    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(
        ftplib,
        "FTP",
        lambda: ConnectableFTPSession(opts_error=True, opts_error_reply=reply),
    )
    transport = FTPTransport(ftp_target())
    transport.connect()
    with pytest.raises(DeployError, match="OPTS UTF8 ON failed"):
        transport.enable_utf8()


def test_ftp_reconnect_opts_temp_failure_stops_before_business_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary OPTS failure still fails closed during sticky reconnect."""

    first = ConnectableFTPSession()
    second = ConnectableFTPSession(opts_temp_error=True)
    sessions = [first, second]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = FTPTransport(ftp_target())
    transport.connect()
    transport.enable_utf8()
    transport.close()

    with pytest.raises(DeployError, match="OPTS UTF8 ON failed"):
        transport.connect()

    assert second.commands == ["FEAT", "OPTS UTF8 ON"]
    assert second.closed
    assert transport.ftp is None


def test_ftp_enable_utf8_requires_feat_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing FEAT UTF8 remains a hard failure even without OPTS."""

    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: ConnectableFTPSession(feat_utf8=False))
    transport = FTPTransport(ftp_target())
    transport.connect()

    with pytest.raises(DeployError, match="mandatory UTF8"):
        transport.enable_utf8()


def test_normalize_ftp_server_banner_redacts_pureftpd_volatile_fields() -> None:
    """User-count and local-time values must not affect banner identity."""

    from git_deploy.transports.ftp import normalize_ftp_server_banner

    morning = (
        "220---------- Welcome to Pure-FTPd [privsep] [TLS] ----------\n"
        "220-You are user number 1 of 50 allowed.\n"
        "220-Local time is now 09:01. Server port: 21.\n"
        "220-This is a private system - No anonymous login\n"
        "220 You will be disconnected after 15 minutes of inactivity."
    )
    evening = (
        "220---------- Welcome to Pure-FTPd [privsep] [TLS] ----------\n"
        "220-You are user number 12 of 50 allowed.\n"
        "220-Local time is now 18:48. Server port: 21.\n"
        "220-This is a private system - No anonymous login\n"
        "220 You will be disconnected after 15 minutes of inactivity."
    )
    redacted = normalize_ftp_server_banner(morning)
    assert redacted == normalize_ftp_server_banner(evening)
    # Field redaction keeps structure and stable suffixes; volatile numbers go.
    assert "user number <n> of <n> allowed" in redacted.lower()
    assert "local time is now <time>" in redacted.lower()
    assert "Server port: 21" in redacted
    assert "09:01" not in redacted
    assert "18:48" not in redacted
    assert "Welcome to Pure-FTPd" in redacted


def test_normalize_ftp_server_banner_keeps_stable_suffix_on_volatile_line() -> None:
    """Stable tokens on a local-time line must still participate in identity."""

    from git_deploy.transports.ftp import normalize_ftp_server_banner

    a = "220-Local time is now 09:01. Server port: 21. Node: ftp-a."
    b = "220-Local time is now 18:48. Server port: 21. Node: ftp-a."
    c = "220-Local time is now 18:48. Server port: 21. Node: ftp-b."
    assert normalize_ftp_server_banner(a) == normalize_ftp_server_banner(b)
    assert "Node: ftp-a" in normalize_ftp_server_banner(a)
    assert normalize_ftp_server_banner(a) != normalize_ftp_server_banner(c)


def test_ftp_server_banner_hash_rejects_empty_stable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty or whitespace-only welcome must fail closed for identity."""

    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: ConnectableFTPSession(banner=""))
    transport = FTPTransport(ftp_target())
    transport.connect()
    with pytest.raises(DeployError, match="lacks stable identity material"):
        transport.server_banner_hash()


def test_ftp_server_banner_hash_stable_across_pureftpd_volatile_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability-profile identity stays fixed when Pure-FTPd rewrites the clock."""

    class FixedBannerSession(ConnectableFTPSession):
        """Connectable session with an explicit Pure-FTPd-style welcome."""

        def __init__(self, banner: str) -> None:
            """Store one welcome string for hashing tests."""

            super().__init__()
            self._fixed_banner = banner

        def getwelcome(self) -> str:
            """Return the configured Pure-FTPd-style banner."""

            return self._fixed_banner

    morning = (
        "220---------- Welcome to Pure-FTPd [privsep] [TLS] ----------\n"
        "220-You are user number 1 of 50 allowed.\n"
        "220-Local time is now 09:01. Server port: 21.\n"
        "220-This is a private system - No anonymous login"
    )
    evening = (
        "220---------- Welcome to Pure-FTPd [privsep] [TLS] ----------\n"
        "220-You are user number 3 of 50 allowed.\n"
        "220-Local time is now 18:48. Server port: 21.\n"
        "220-This is a private system - No anonymous login"
    )
    sessions = [FixedBannerSession(morning), FixedBannerSession(evening)]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = FTPTransport(ftp_target())
    transport.connect()
    hash_a = transport.server_banner_hash()
    transport.close()
    transport.connect()
    hash_b = transport.server_banner_hash()
    assert hash_a == hash_b
    assert len(hash_a) == 64


def test_ftp_reconnect_banner_drift_fails_before_utf8_or_business_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed banner prevents negotiation and all later remote operations."""

    first = ConnectableFTPSession()
    second = ConnectableFTPSession(banner="220 changed")
    sessions = [first, second]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = FTPTransport(ftp_target())
    transport.connect()
    transport.enable_utf8()
    transport.close()

    with pytest.raises(DeployError, match="banner changed"):
        transport.connect()

    assert second.commands == []
    assert second.closed


@pytest.mark.parametrize("kind", ("source-upload", "output-upload", "delete"))
def test_unicode_business_retry_restores_utf8_before_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    """Unicode source upload and delete retries run only after fresh OPTS."""

    first = ConnectableFTPSession()
    second = ConnectableFTPSession()
    sessions = [first, second]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = RetryingUnicodeFTPTransport(ftp_target())
    transport.connect()
    transport.enable_utf8()
    path = "部署/文件.txt"
    frozen: dict[str, Path] = {}
    if kind.endswith("upload"):
        local = tmp_path / "payload"
        local.write_bytes(b"payload")
        frozen[path] = local
        origin = "source" if kind == "source-upload" else "output"
        operation = UploadOperation(path, origin, local_path=local)
    else:
        operation = DeleteOperation(path, "source")

    _execute_with_retry(
        operation,
        frozen,
        transport,
        ProgressReporter(False),
        attempts=2,
        delay=0,
    )

    assert second.commands == ["FEAT", "OPTS UTF8 ON"]
    assert (
        transport.upload_attempts if kind.endswith("upload") else transport.delete_attempts
    ) == 2


@pytest.mark.parametrize("label", ("Hybrid Stage", "Hybrid Publish", "Unicode RMD"))
def test_ftp_hybrid_mutation_retry_restores_utf8_before_action(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    """Stage, Publish, and RMD retry paths inherit the sticky UTF-8 session."""

    first = ConnectableFTPSession()
    second = ConnectableFTPSession()
    sessions = [first, second]
    monkeypatch.setenv("PASSWORD", "secret")
    monkeypatch.setattr(ftplib, "FTP", lambda: sessions.pop(0))
    transport = RetryingUnicodeFTPTransport(ftp_target())
    transport.connect()
    transport.enable_utf8()
    attempts = 0

    def action() -> None:
        """Fail once and prove each attempt observes an activated UTF-8 session."""

        nonlocal attempts
        assert transport._require_ftp().encoding == "utf-8"
        attempts += 1
        if attempts == 1:
            raise OSError("transient Hybrid mutation failure")

    _retry_ftp_mutation(
        label,
        action,
        transport,
        attempts=2,
        delay=0,
    )

    assert attempts == 2
    assert second.commands == ["FEAT", "OPTS UTF8 ON"]


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
    progress: list[tuple[int, int | None]] = []

    transport.delete("absent.txt")
    transport.upload(payload, "new.txt", lambda done, total: progress.append((done, total)))
    transport.delete("new.txt")
    transport.delete("new.txt")

    assert fake.nlst_calls == ["/root"]
    assert fake.deleted == ["/root/new.txt"]
    assert progress == [(0, 3), (3, 3)]
    transport.close()
    assert transport._directory_entries == {}


def test_ftp_upload_timer_starts_after_parent_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FTP directory preparation is excluded from active STOR time."""

    now = 100.0

    def clock() -> float:
        """Return the mutable synthetic monotonic time."""

        return now

    fake = CachingFTP({"/": {"root"}, "/root": set()})
    original_store = fake.storbinary

    def mkdirs(_path: str) -> None:
        """Model five seconds of FTP parent preparation."""

        nonlocal now
        now += 5

    def storbinary(command, handle, *, blocksize, callback):  # noqa: ANN001, ANN202
        """Model one second of STOR before its cumulative callback."""

        nonlocal now
        now += 1
        return original_store(command, handle, blocksize=blocksize, callback=callback)

    fake.storbinary = storbinary  # type: ignore[method-assign]
    transport = FTPTransport(ftp_target())
    transport.ftp = fake  # type: ignore[assignment]
    monkeypatch.setattr(transport, "_mkdirs", mkdirs)
    payload = tmp_path / "timed.bin"
    payload.write_bytes(b"payload")
    reporter = ProgressReporter(clock=clock)

    transport.upload(payload, "timed.bin", reporter.callback("timed.bin", 7))

    summary = reporter.finish()
    assert summary is not None
    assert summary.active_seconds == pytest.approx(1)


def test_paramiko_upload_timer_starts_after_parent_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paramiko directory preparation is excluded from active put time."""

    now = 100.0
    callbacks: list[tuple[int, int]] = []

    def clock() -> float:
        """Return the mutable synthetic monotonic time."""

        return now

    class UploadSFTP:
        """Provide only the Paramiko upload calls used by this test."""

        def put(self, local, remote, *, callback, confirm):  # noqa: ANN001, ANN201
            """Advance upload time and publish one cumulative callback."""

            nonlocal now
            del remote, confirm
            now += 1
            size = Path(local).stat().st_size
            callbacks.append((size, size))
            callback(size, size)

        def chmod(self, path: str, mode: int) -> None:
            """Accept temporary-file mode publication."""

        def remove(self, path: str) -> None:
            """Accept best-effort cleanup if an assertion fails."""

    def mkdirs(_path: str) -> None:
        """Model five seconds of Paramiko parent preparation."""

        nonlocal now
        now += 5

    transport = SFTPTransport(sftp_target())
    transport.sftp = UploadSFTP()  # type: ignore[assignment]
    monkeypatch.setattr(transport, "_mkdirs", mkdirs)
    monkeypatch.setattr(transport, "_publish_temporary", lambda source, target: None)
    payload = tmp_path / "timed.bin"
    payload.write_bytes(b"payload")
    reporter = ProgressReporter(clock=clock)
    reported: list[tuple[int, int | None]] = []
    callback = reporter.callback("timed.bin", 7)

    def record(done: int, total: int | None = None) -> None:
        """Record transport signals before forwarding them to the Reporter."""

        reported.append((done, total))
        callback(done, total)

    transport.upload(payload, "timed.bin", record)

    summary = reporter.finish()
    assert callbacks == [(7, 7)]
    assert reported == [(0, 7), (7, 7)]
    assert summary is not None
    assert summary.active_seconds == pytest.approx(1)


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
