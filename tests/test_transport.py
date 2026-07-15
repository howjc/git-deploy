"""SSH alias and exact agent-key selection tests."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError, GitDeployError
from git_deploy.remote_permissions import SftpPermissionPolicy
from git_deploy.transport import SftpTransport, _agent_key_for_public_file, resolve_server


class _FakeSftpHandle:
    """Capture streamed SFTP bytes for permission-policy tests."""

    def __init__(self, actions: list[tuple[object, ...]], path: str):
        """Bind an action sink and temporary path."""

        self.actions = actions
        self.path = path

    def __enter__(self) -> _FakeSftpHandle:
        """Return this context-managed fake handle."""

        return self

    def __exit__(self, *args: object) -> None:
        """Close the fake handle without suppressing exceptions."""

        del args

    def write(self, data: bytes) -> None:
        """Record one uploaded chunk."""

        self.actions.append(("write", self.path, data))


class _FakeSftpClient:
    """Minimal SFTP surface that records mutation ordering."""

    def __init__(self) -> None:
        """Start with only the deployment parent directory present."""

        self.actions: list[tuple[object, ...]] = []
        self.directories = {"/srv"}

    def stat(self, path: str) -> object:
        """Return an object for existing directories or report absence."""

        self.actions.append(("stat", path))
        if path not in self.directories:
            raise FileNotFoundError(path)
        return object()

    def mkdir(self, path: str) -> None:
        """Record and create one directory."""

        self.actions.append(("mkdir", path))
        self.directories.add(path)

    def chmod(self, path: str, mode: int) -> None:
        """Record a POSIX mode update."""

        self.actions.append(("chmod", path, mode))

    def open(self, path: str, mode: str) -> _FakeSftpHandle:
        """Return a writable fake SFTP handle."""

        self.actions.append(("open", path, mode))
        return _FakeSftpHandle(self.actions, path)

    def posix_rename(self, source: str, target: str) -> None:
        """Record one atomic replacement."""

        self.actions.append(("posix_rename", source, target))

    def remove(self, path: str) -> None:
        """Record cleanup of a failed temporary upload."""

        self.actions.append(("remove", path))


def _fake_sftp_transport(
    policy: SftpPermissionPolicy,
) -> tuple[SftpTransport, _FakeSftpClient, list[str]]:
    """Create a disconnected transport with deterministic fake SFTP/SSH surfaces."""

    transport = SftpTransport.__new__(SftpTransport)
    client = _FakeSftpClient()
    commands: list[str] = []
    transport._sftp = client
    transport._permissions = policy
    transport._directories = set()
    transport._posix_rename_supported = None

    def execute(command: str) -> tuple[int, str, str]:
        """Capture a successful ownership command."""

        commands.append(command)
        return 0, "", ""

    transport.execute = execute  # type: ignore[method-assign]
    return transport, client, commands


def test_resolve_server_uses_effective_openssh_alias_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read host, user, port, identities, and known-hosts from ``ssh -G`` output."""

    config = tmp_path / "config"
    config.write_text("Host production\n", encoding="utf-8")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return representative effective OpenSSH configuration.

        Args:
            args: Subprocess positional arguments, unused.
            kwargs: Subprocess keyword arguments, unused.

        Returns:
            Successful text subprocess result.
        """

        del args, kwargs
        return subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout=(
                "hostname 192.0.2.10\n"
                "user deploy\n"
                "port 2222\n"
                "identityfile ~/.ssh/production.pub\n"
                "userknownhostsfile ~/.ssh/known_hosts\n"
                "proxyjump none\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolved = resolve_server(
        {
            "protocol": "sftp",
            "ssh_host_alias": "production",
            "ssh_config_file": str(config),
        }
    )

    assert resolved["host"] == "192.0.2.10"
    assert resolved["username"] == "deploy"
    assert resolved["port"] == 2222
    assert resolved["key_file"] == ["~/.ssh/production.pub"]
    assert resolved["known_hosts_file"] == "~/.ssh/known_hosts"


def test_resolve_server_refuses_unsupported_proxy_jump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed instead of silently bypassing an SSH jump host."""

    config = tmp_path / "config"
    config.write_text("Host production\n", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout="hostname 192.0.2.10\nproxyjump bastion\n",
            stderr="",
        ),
    )

    with pytest.raises(ConfigurationError, match="unsupported proxyjump"):
        resolve_server(
            {
                "protocol": "sftp",
                "ssh_host_alias": "production",
                "ssh_config_file": str(config),
            }
        )


def test_sftp_ownership_and_permissions_precede_atomic_replace() -> None:
    """Publish files only after modes/ownership and configure newly made directories."""

    policy = SftpPermissionPolicy(
        owner="www-data",
        group="web",
        file_mode=0o640,
        executable_mode=0o750,
        directory_mode=0o750,
    )
    transport, client, commands = _fake_sftp_transport(policy)

    transport.replace_file("/srv/app/config.php", b"payload")

    temporary = next(
        action[1]
        for action in client.actions
        if action[0] == "open"
    )
    assert ("mkdir", "/srv/app") in client.actions
    assert ("chmod", "/srv/app", 0o750) in client.actions
    assert ("chmod", temporary, 0o640) in client.actions
    assert commands == [
        "chown www-data:web /srv/app",
        f"chown www-data:web {temporary}",
    ]
    rename_index = client.actions.index(
        ("posix_rename", temporary, "/srv/app/config.php")
    )
    chmod_index = client.actions.index(("chmod", temporary, 0o640))
    assert chmod_index < rename_index


def test_sftp_permission_defaults_preserve_executable_mode() -> None:
    """Default regular/executable files to 0644/0755 and directories to 0755."""

    transport, client, commands = _fake_sftp_transport(SftpPermissionPolicy())

    transport.replace_file("/srv/bin/tool", b"#!/bin/sh", executable=True)

    temporary = next(
        action[1]
        for action in client.actions
        if action[0] == "open"
    )
    assert ("chmod", "/srv/bin", 0o755) in client.actions
    assert ("chmod", temporary, 0o755) in client.actions
    assert commands == []


def test_sftp_ownership_supports_group_only_policy() -> None:
    """Use chgrp when only a destination group is configured."""

    transport, _, commands = _fake_sftp_transport(
        SftpPermissionPolicy(group="web")
    )

    transport.replace_file("/srv/shared/file.txt", b"data")

    assert commands[0] == "chgrp web /srv/shared"
    assert commands[1].startswith("chgrp web /srv/shared/file.txt.git-deploy-")


def test_sftp_ownership_failure_cleans_temp_without_publishing() -> None:
    """Abort and remove the temporary file when chown fails before rename."""

    transport, client, _ = _fake_sftp_transport(
        SftpPermissionPolicy(owner="www-data", group="web")
    )

    def fail_file_ownership(command: str) -> tuple[int, str, str]:
        """Allow directory ownership but reject temporary-file ownership."""

        if ".git-deploy-" in command:
            return 1, "", "permission denied"
        return 0, "", ""

    transport.execute = fail_file_ownership  # type: ignore[method-assign]

    with pytest.raises(GitDeployError, match="cannot set remote ownership"):
        transport.replace_file("/srv/app/file.txt", b"data")

    assert not any(action[0] == "posix_rename" for action in client.actions)
    assert any(action[0] == "remove" for action in client.actions)


class _NoPosixRenameSftpClient(_FakeSftpClient):
    """Fake client whose ``posix_rename`` always reports the extension itself
    as unsupported, and whose plain ``rename``/``stat`` model live targets."""

    def __init__(self) -> None:
        super().__init__()
        self.targets: set[str] = set()

    def stat(self, path: str) -> object:
        self.actions.append(("stat", path))
        if path in self.directories or path in self.targets:
            return object()
        raise FileNotFoundError(path)

    def posix_rename(self, source: str, target: str) -> None:
        self.actions.append(("posix_rename", source, target))
        raise OSError("SSH_FX_OP_UNSUPPORTED: server does not support this extension")

    def rename(self, source: str, target: str) -> None:
        self.actions.append(("rename", source, target))
        self.targets.discard(source)
        self.targets.add(target)

    def remove(self, path: str) -> None:
        super().remove(path)
        self.targets.discard(path)


class _GenericFailureSftpClient(_NoPosixRenameSftpClient):
    """Fake client whose ``posix_rename`` fails with an ambiguous, generic
    error (no "unsupported"-style wording) — the same shape a real server
    returns for a plain SSH_FX_FAILURE, which paramiko cannot distinguish
    from a genuinely-missing extension by errno alone."""

    def posix_rename(self, source: str, target: str) -> None:
        self.actions.append(("posix_rename", source, target))
        raise OSError("Failure")


def test_sftp_posix_rename_unsupported_falls_back_to_safe_backup_swap() -> None:
    """P1-06: server-reported unsupported extension uses a non-destructive fallback."""

    transport = SftpTransport.__new__(SftpTransport)
    client = _NoPosixRenameSftpClient()
    client.directories.add("/srv/app")
    client.targets.add("/srv/app/config.php")
    transport._sftp = client
    transport._permissions = SftpPermissionPolicy()
    transport._directories = {"/srv/app"}
    transport._posix_rename_supported = None
    transport.execute = lambda command: (0, "", "")  # type: ignore[method-assign]

    transport.replace_file("/srv/app/config.php", b"payload")

    assert transport._posix_rename_supported is False
    rename_targets = [action[2] for action in client.actions if action[0] == "rename"]
    # remote_path -> backup, then temporary -> remote_path (never a blind delete).
    assert rename_targets[0].startswith("/srv/app/config.php.git-deploy-")
    assert rename_targets[1] == "/srv/app/config.php"
    assert not any(
        action[0] == "remove" and action[1] == "/srv/app/config.php"
        for action in client.actions
    )
    assert client.targets == {"/srv/app/config.php"}

    # A second publish on the same connection must skip posix_rename entirely.
    client.actions.clear()
    transport.replace_file("/srv/app/config.php", b"payload-2")
    assert not any(action[0] == "posix_rename" for action in client.actions)


def test_sftp_posix_rename_generic_failure_still_falls_back_safely() -> None:
    """P1-06: an ambiguous/generic posix_rename failure (no "unsupported"
    wording) must not be treated differently from an explicit one — it still
    uses the non-destructive backup-swap instead of permanently hard-failing
    every future write on this connection."""

    transport = SftpTransport.__new__(SftpTransport)
    client = _GenericFailureSftpClient()
    client.directories.add("/srv/app")
    client.targets.add("/srv/app/config.php")
    transport._sftp = client
    transport._permissions = SftpPermissionPolicy()
    transport._directories = {"/srv/app"}
    transport._posix_rename_supported = None
    transport.execute = lambda command: (0, "", "")  # type: ignore[method-assign]

    transport.replace_file("/srv/app/config.php", b"payload")

    assert transport._posix_rename_supported is False
    rename_targets = [action[2] for action in client.actions if action[0] == "rename"]
    assert rename_targets[0].startswith("/srv/app/config.php.git-deploy-")
    assert rename_targets[1] == "/srv/app/config.php"
    assert client.targets == {"/srv/app/config.php"}

    # A second write must not hard-fail forever after one ambiguous failure.
    client.actions.clear()
    transport.replace_file("/srv/app/config.php", b"payload-2")
    assert not any(action[0] == "posix_rename" for action in client.actions)


def test_public_identity_selects_only_matching_agent_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match a public IdentityFile fingerprint to exactly one agent key."""

    public_blob = b"ssh-ed25519-test-public-blob"
    public_file = tmp_path / "production.pub"
    public_file.write_text(
        "ssh-ed25519 " + base64.b64encode(public_blob).decode("ascii") + " test\n",
        encoding="utf-8",
    )

    class FakeKey:
        """Expose a Paramiko-compatible legacy fingerprint."""

        def __init__(self, fingerprint: bytes):
            """Store the key fingerprint.

            Args:
                fingerprint: MD5 identity digest.
            """

            self.fingerprint = fingerprint

        def get_fingerprint(self) -> bytes:
            """Return the configured identity digest.

            Returns:
                Fingerprint bytes.
            """

            return self.fingerprint

    wrong = FakeKey(b"x" * 16)
    matching = FakeKey(hashlib.md5(public_blob).digest())  # noqa: S324 - identity comparison.

    class FakeAgent:
        """Provide deterministic keys and retain close state."""

        def __init__(self) -> None:
            """Initialize an open fake agent."""

            self.closed = False

        def get_keys(self) -> tuple[FakeKey, FakeKey]:
            """Return one wrong and one matching identity.

            Returns:
                Ordered fake agent keys.
            """

            return wrong, matching

        def close(self) -> None:
            """Record agent closure."""

            self.closed = True

    fake_module = types.SimpleNamespace(Agent=FakeAgent)
    monkeypatch.setitem(sys.modules, "paramiko", fake_module)

    key, agent = _agent_key_for_public_file(public_file)

    assert key is matching
    assert agent is not None
    assert not agent.closed


def test_build_ftps_ssl_context_verified_by_default() -> None:
    """Default FTPS context requires certificates and hostname checks."""

    import ssl

    from git_deploy.transport import build_ftps_ssl_context, ftps_tls_trust_digest

    context = build_ftps_ssl_context({"host": "ftp.example.com"})
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    digest = ftps_tls_trust_digest({"host": "ftp.example.com"})
    assert len(digest) == 64
    # Trust digest must not embed secrets.
    secret_digest = ftps_tls_trust_digest(
        {"host": "ftp.example.com", "password": "super-secret", "tls_key_file": "/k.pem"}
    )
    assert "super-secret" not in secret_digest
    # key_file path is intentionally excluded from digest (private material surface).
    assert ftps_tls_trust_digest({"tls_verify": True}) == ftps_tls_trust_digest(
        {"tls_verify": True, "password": "x", "tls_key_file": "/secret.pem"}
    )


def test_build_ftps_ssl_context_insecure_opt_out_warns() -> None:
    """Explicit tls_verify=false disables verification and emits a security warning."""

    import ssl
    import warnings

    from git_deploy.transport import build_ftps_ssl_context, ftps_tls_trust_digest

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        context = build_ftps_ssl_context({"tls_verify": False})
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False
    assert any("tls_verify=false" in str(item.message) for item in caught)
    assert ftps_tls_trust_digest({"tls_verify": False}) != ftps_tls_trust_digest(
        {"tls_verify": True}
    )


def test_build_ftps_ssl_context_custom_ca(tmp_path: Path) -> None:
    """tls_ca_file is loaded into a verified default context."""

    import ssl
    from datetime import datetime, timedelta, timezone

    from git_deploy.transport import build_ftps_ssl_context

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pytest.skip("cryptography package required for CA fixture")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = build_ftps_ssl_context({"tls_ca_file": str(ca_path), "tls_verify": True})
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_build_ftps_ssl_context_corrupt_ca_pem_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """P1-08: a corrupt CA PEM must surface as ConfigurationError, not a raw ssl.SSLError."""

    from git_deploy.transport import build_ftps_ssl_context

    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(b"not a real certificate")

    with pytest.raises(ConfigurationError, match="invalid FTPS TLS configuration"):
        build_ftps_ssl_context({"tls_ca_file": str(ca_path), "tls_verify": True})


def test_build_ftps_ssl_context_missing_ca_file_raises_configuration_error(
    tmp_path: Path,
) -> None:
    """P1-08: a CA file that vanished between config load and connect is still
    reported as a structured configuration error."""

    from git_deploy.transport import build_ftps_ssl_context

    missing = tmp_path / "does-not-exist.pem"

    with pytest.raises(ConfigurationError, match="invalid FTPS TLS configuration"):
        build_ftps_ssl_context({"tls_ca_file": str(missing), "tls_verify": True})


def test_ftps_connect_rejects_untrusted_certificate(tmp_path: Path) -> None:
    """FtpTransport with verified context fails against an untrusted self-signed server."""

    import ipaddress
    import socket
    import ssl
    import threading
    from datetime import datetime, timedelta, timezone

    from git_deploy.errors import GitDeployError
    from git_deploy.transport import FtpTransport

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pytest.skip("cryptography package required for TLS fixture")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    # Minimal TLS-wrapped socket that completes handshake then closes — enough to
    # exercise FTPS client certificate verification before FTP protocol exchange.
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert_path), str(key_path))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        """Accept one TLS connection and drop it."""

        listener.settimeout(2.0)
        try:
            conn, _addr = listener.accept()
            try:
                with server_ctx.wrap_socket(conn, server_side=True) as tls_conn:
                    tls_conn.recv(1)
            except Exception:
                pass
            finally:
                conn.close()
        except Exception:
            pass
        finally:
            stop.set()
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(GitDeployError, match="FTP connection failed|certificate|SSL|ssl"):
            FtpTransport(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "username": "u",
                    "password": "p",
                    "timeout": 2,
                    "tls_verify": True,
                },
                use_tls=True,
            )
    finally:
        stop.wait(timeout=3)
        thread.join(timeout=3)


def test_ftps_hostname_mismatch_fails_with_verified_context(tmp_path: Path) -> None:
    """Verified context rejects a certificate whose hostname does not match."""

    import ssl

    from git_deploy.transport import build_ftps_ssl_context

    # Hostname checking is enabled on the verified context; mismatch is enforced
    # by ssl when wrap_socket(server_hostname=...) does not match the cert SAN/CN.
    context = build_ftps_ssl_context({"tls_verify": True})
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def _generate_test_certificate(
    tmp_path: Path,
    name: str,
    *,
    common_name: str = "127.0.0.1",
    san_ip: str | None = "127.0.0.1",
    not_before_days: int = -1,
    not_after_days: int = 30,
):
    """Write a self-signed cert/key pair and return (cert_path, key_path).

    Args:
        tmp_path: Directory to write the PEM files into.
        name: File name stem (``{name}.pem`` / ``{name}-key.pem``).
        common_name: Certificate subject/issuer common name.
        san_ip: Optional SAN IP address entry (``None`` omits the extension).
        not_before_days: Validity start, in days relative to now.
        not_after_days: Validity end, in days relative to now.

    Returns:
        Tuple of written certificate and private key paths.
    """

    import ipaddress
    from datetime import datetime, timedelta, timezone

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        pytest.skip("cryptography package required for TLS fixture")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) + timedelta(days=not_before_days))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=not_after_days))
    )
    if san_ip is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address(san_ip))]),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    cert_path = tmp_path / f"{name}.pem"
    key_path = tmp_path / f"{name}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _RealFtpsServer:
    """A real explicit-FTPS server (pyftpdlib) for protocol-level tests.

    Requires TLS on both control and data channels, exactly like a hardened
    production FTPS host — this is what proves git-deploy's client actually
    completes AUTH TLS / PBSZ / PROT P and a TLS-wrapped data channel, not
    just that an ``ssl.SSLContext`` was built with the right flags.
    """

    def __init__(self, tmp_path: Path, cert_path: Path, key_path: Path):
        """Start serving ``tmp_path/ftproot`` over explicit FTPS on 127.0.0.1.

        Args:
            tmp_path: Test temp directory (home dir lives under it).
            cert_path: Server certificate PEM.
            key_path: Server private key PEM.
        """

        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers.ftps.control import TLS_FTPHandler
            from pyftpdlib.servers import FTPServer
        except ImportError:
            pytest.skip("pyftpdlib/pyOpenSSL required for real FTPS protocol tests")

        import threading

        self.home_dir = tmp_path / "ftproot"
        self.home_dir.mkdir(exist_ok=True)
        authorizer = DummyAuthorizer()
        authorizer.add_user("u", "p", str(self.home_dir), perm="elradfmwMT")

        handler = type("_TestTLSHandler", (TLS_FTPHandler,), {})
        handler.certfile = str(cert_path)
        handler.keyfile = str(key_path)
        handler.tls_control_required = True
        handler.tls_data_required = True
        handler.authorizer = authorizer
        handler.passive_ports = range(60100, 60200)
        handler.banner = "test-ftps-server ready"

        self._server = FTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the background server thread."""

        self._server.close_all()
        self._thread.join(timeout=3)

    def __enter__(self) -> "_RealFtpsServer":
        """Return self for ``with`` usage."""

        return self

    def __exit__(self, *args: object) -> None:
        """Stop the server on context exit."""

        del args
        self.close()


def test_ftps_full_roundtrip_over_real_tls_server(tmp_path: Path) -> None:
    """P1-07: trusted CA + matching SAN completes a real AUTH TLS handshake,
    PROT P data channel, and full STOR/RETR/DELE roundtrip."""

    from git_deploy.transport import FtpTransport

    cert_path, key_path = _generate_test_certificate(tmp_path, "server")
    with _RealFtpsServer(tmp_path, cert_path, key_path) as server:
        transport = FtpTransport(
            {
                "host": "127.0.0.1",
                "port": server.port,
                "username": "u",
                "password": "p",
                "timeout": 5,
                "tls_ca_file": str(cert_path),
                "tls_verify": True,
                "passive": True,
            },
            use_tls=True,
        )
        try:
            payload = b"real ftps roundtrip payload" * 100
            transport.replace_file("/upload.txt", payload)
            assert (server.home_dir / "upload.txt").read_bytes() == payload
            assert transport.read_file("/upload.txt") == payload
            # P1-06: overwrite an existing file through the real server —
            # exercises the non-destructive backup-swap branch, not just the
            # fresh-path STOR/rename above.
            replacement = b"replaced content over an existing live file"
            transport.replace_file("/upload.txt", replacement)
            assert (server.home_dir / "upload.txt").read_bytes() == replacement
            assert transport.read_file("/upload.txt") == replacement
            leftover = [p.name for p in server.home_dir.iterdir() if p.name != "upload.txt"]
            assert leftover == [], f"backup/temp files leaked: {leftover}"
            transport.delete_file("/upload.txt")
            assert transport.read_file("/upload.txt") is None
        finally:
            transport.close()


def test_ftps_expired_certificate_rejected_by_real_server(tmp_path: Path) -> None:
    """P1-07: a verified context refuses an actually-expired server certificate."""

    from git_deploy.errors import GitDeployError
    from git_deploy.transport import FtpTransport

    cert_path, key_path = _generate_test_certificate(
        tmp_path, "expired", not_before_days=-30, not_after_days=-1
    )
    with _RealFtpsServer(tmp_path, cert_path, key_path) as server:
        with pytest.raises(GitDeployError, match="FTP connection failed|certificate|SSL|ssl"):
            FtpTransport(
                {
                    "host": "127.0.0.1",
                    "port": server.port,
                    "username": "u",
                    "password": "p",
                    "timeout": 5,
                    "tls_ca_file": str(cert_path),
                    "tls_verify": True,
                },
                use_tls=True,
            )


def test_ftps_real_hostname_mismatch_rejected(tmp_path: Path) -> None:
    """P1-07: a verified context refuses a certificate issued for a different host,
    proving the handshake actually enforces hostname checking end to end."""

    from git_deploy.errors import GitDeployError
    from git_deploy.transport import FtpTransport

    cert_path, key_path = _generate_test_certificate(
        tmp_path,
        "wrong-host",
        common_name="not-the-server.invalid",
        san_ip="10.255.255.254",
    )
    with _RealFtpsServer(tmp_path, cert_path, key_path) as server:
        with pytest.raises(GitDeployError, match="FTP connection failed|certificate|SSL|ssl"):
            FtpTransport(
                {
                    "host": "127.0.0.1",
                    "port": server.port,
                    "username": "u",
                    "password": "p",
                    "timeout": 5,
                    "tls_ca_file": str(cert_path),
                    "tls_verify": True,
                },
                use_tls=True,
            )


def test_ftps_tls_verify_false_connects_despite_untrusted_certificate(
    tmp_path: Path,
) -> None:
    """P1-07: the documented tls_verify=false escape hatch really does connect
    through a certificate that would otherwise fail verification."""

    from git_deploy.transport import FtpTransport

    cert_path, key_path = _generate_test_certificate(tmp_path, "self-signed")
    with _RealFtpsServer(tmp_path, cert_path, key_path) as server:
        # No tls_ca_file configured: default verification would reject this
        # self-signed certificate outright.
        transport = FtpTransport(
            {
                "host": "127.0.0.1",
                "port": server.port,
                "username": "u",
                "password": "p",
                "timeout": 5,
                "tls_verify": False,
            },
            use_tls=True,
        )
        try:
            transport.replace_file("/insecure.txt", b"data")
            assert transport.read_file("/insecure.txt") == b"data"
        finally:
            transport.close()


@pytest.mark.parametrize("protocol", ["sftp", "ftp", "ftps"])
def test_fake_transport_state_and_rollback_semantics_match(
    tmp_path: Path, protocol: str
) -> None:
    """All protocol facades share identical transaction/state/latest-rollback behavior."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import PlannedFile, ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
    from git_deploy.state_planner import SourceDiffPlan
    from git_deploy.state_rollback import StateRollbackService
    from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "app.txt").write_bytes(b"old")
    subprocess.run(["git", "-C", str(repo), "add", "app.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "old"], check=True)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    project = ProjectConfig("demo", repo, "/srv")
    identity = resolve_target_identity({"protocol": protocol, "host": "fake"}, project)
    root = tmp_path / "target"
    git_store = PersistentGitStore(root, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old_hash = hashlib.sha256(b"old").hexdigest()
    new_hash = hashlib.sha256(b"new").hexdigest()
    ContentAddressedStore(root).put(b"old")
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry("app.txt", "source", old_hash),),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=state)
    remote = InMemoryTransport()
    remote.files["/srv/app.txt"] = FakeRemotePath(b"old")
    operation = PlannedFile(
        "upload", "app.txt", "/srv/app.txt", "app.txt", old_hash, new_hash, 3
    )
    plan = SourceDiffPlan(tree, tree, (operation,), (), ("t1",), ("t0", "t1"))
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=remote,
        content_provider=lambda _path: b"new",
    )
    result = executor.deploy(plan, (operation,))
    assert result["status"] == "succeeded"
    assert remote.files["/srv/app.txt"].data == b"new"
    rolled = StateRollbackService(project, identity, root, transport=remote).rollback_latest()
    assert rolled.status == "succeeded"
    assert remote.files["/srv/app.txt"].data == b"old"
    current = store.load_current_state()
    assert current is not None and current[1].generation == 3
