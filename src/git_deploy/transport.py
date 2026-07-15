"""Remote transports with SSH alias and exact agent-key authentication."""

from __future__ import annotations

import base64
import ftplib
import hashlib
import io
import os
import posixpath
import shlex
import stat
import subprocess
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol

from .errors import ConfigurationError, GitDeployError
from .remote_permissions import load_sftp_permission_policy


ByteProgress = Callable[[int], None]
TRANSFER_BLOCK_SIZE = 256 * 1024


class RemoteTransport(Protocol):
    """File operations required by deploy, verification, and rollback."""

    supports_commands: bool

    def read_file(
        self,
        remote_path: str,
        progress: ByteProgress | None = None,
    ) -> bytes | None:
        """Return remote bytes or ``None`` when the file does not exist."""

    def is_executable(self, remote_path: str) -> bool | None:
        """Return executable state when the protocol can inspect file modes."""

    def replace_file(
        self,
        remote_path: str,
        data: bytes,
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Upload bytes through a temporary name and replace the destination."""

    def replace_file_stream(
        self,
        remote_path: str,
        chunks: Iterable[bytes],
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Upload bounded chunks through a temporary sibling and replace."""

    def list_files(self, remote_prefix: str) -> tuple[str, ...]:
        """List regular/non-directory paths at or below a remote prefix."""

    def delete_file(self, remote_path: str) -> None:
        """Delete a remote file when present."""

    def execute(self, command: str) -> tuple[int, str, str]:
        """Run one remote command and return exit code, stdout, and stderr."""

    def close(self) -> None:
        """Release network resources."""


def resolve_server(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve an optional OpenSSH alias while preserving explicit TOML values.

    Args:
        raw: Parsed ``[server]`` table.

    Returns:
        New server mapping containing effective host, port, user, and identities.
    """

    server = dict(raw)
    protocol = str(server.get("protocol", "sftp")).lower()
    alias = str(server.get("ssh_host_alias", "")).strip()
    if protocol != "sftp" or not alias:
        return server

    config_path = Path(str(server.get("ssh_config_file", "~/.ssh/config"))).expanduser()
    if not config_path.is_file():
        raise ConfigurationError(f"SSH config does not exist: {config_path}")
    try:
        proc = subprocess.run(
            ["ssh", "-G", "-F", str(config_path), alias],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ConfigurationError(f"cannot resolve SSH alias {alias}: {exc}") from exc
    if proc.returncode != 0:
        raise ConfigurationError(
            f"ssh -G {alias} failed: {proc.stderr.strip() or 'unknown error'}"
        )

    resolved: dict[str, str] = {}
    identities: list[str] = []
    for line in proc.stdout.splitlines():
        key, separator, value = line.strip().partition(" ")
        if not separator:
            continue
        key = key.lower()
        if key == "identityfile":
            identities.append(value)
        elif key in {"proxyjump", "proxycommand"} and value not in {"", "none"}:
            raise ConfigurationError(
                f"SSH alias {alias} uses unsupported {key}={value}; direct fallback is refused"
            )
        elif key not in resolved:
            resolved[key] = value

    server.setdefault("host", resolved.get("hostname", alias))
    server.setdefault("port", int(resolved.get("port", "22")))
    server.setdefault("username", resolved.get("user", ""))
    if "key_file" not in server and identities:
        server["key_file"] = identities
    if "known_hosts_file" not in server:
        known_hosts = resolved.get("userknownhostsfile", "").split()
        if known_hosts:
            server["known_hosts_file"] = known_hosts[0]
    return server


def open_transport(raw_server: dict[str, Any]) -> RemoteTransport:
    """Open the configured SFTP, FTP, or FTPS transport.

    Args:
        raw_server: Parsed, unresolved server configuration.

    Returns:
        Connected transport instance.
    """

    server = resolve_server(raw_server)
    protocol = str(server.get("protocol", "sftp")).lower()
    if protocol == "sftp":
        return SftpTransport(server)
    if protocol == "ftp":
        if not bool(server.get("allow_insecure_ftp", False)):
            raise ConfigurationError(
                "plain FTP is disabled; use FTPS/SFTP or set allow_insecure_ftp=true explicitly"
            )
        return FtpTransport(server, use_tls=False)
    if protocol == "ftps":
        return FtpTransport(server, use_tls=True)
    raise ConfigurationError(f"unsupported protocol: {protocol}")


class SftpTransport:
    """Paramiko SFTP transport with exact 1Password agent-key matching."""

    supports_commands = True

    def __init__(self, server: dict[str, Any]):
        """Connect using explicit credentials or a resolved SSH alias.

        Args:
            server: Effective SFTP connection mapping.
        """

        self._permissions = load_sftp_permission_policy(server)
        try:
            import paramiko
        except ModuleNotFoundError as exc:
            raise ConfigurationError("SFTP requires the paramiko package") from exc

        host = str(server.get("host", "")).strip()
        username = str(server.get("username", "")).strip()
        if not host or not username:
            raise ConfigurationError("SFTP requires host and username or a resolvable SSH alias")

        self._ssh = paramiko.SSHClient()
        strict = bool(server.get("strict_host_key_checking", True))
        if strict:
            self._ssh.load_system_host_keys()
            known_hosts = server.get("known_hosts_file")
            if known_hosts:
                known_path = Path(str(known_hosts)).expanduser()
                if known_path.is_file():
                    self._ssh.load_host_keys(str(known_path))
            self._ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        from .target_identity import effective_port

        password = _resolve_password(server)
        connect: dict[str, Any] = {
            "hostname": host,
            "port": effective_port("sftp", server.get("port")),
            "username": username,
            "timeout": int(server.get("timeout", 15)),
            "allow_agent": bool(server.get("use_ssh_agent", False)),
            "look_for_keys": bool(server.get("use_ssh_agent", False)),
        }

        key_value = server.get("key_file")
        key_paths = key_value if isinstance(key_value, list) else [key_value] if key_value else []
        agent_key = None
        self._agent = None
        private_paths: list[str] = []
        had_public_identity = False
        for raw_path in key_paths:
            path = Path(str(raw_path)).expanduser()
            if _is_public_key(path):
                had_public_identity = True
                if agent_key is None:
                    agent_key, self._agent = _agent_key_for_public_file(path)
            elif path.is_file():
                private_paths.append(str(path))

        if agent_key is not None:
            connect.update(pkey=agent_key, allow_agent=False, look_for_keys=False)
        elif private_paths:
            connect["key_filename"] = private_paths if len(private_paths) > 1 else private_paths[0]
            if password:
                connect["passphrase"] = password
        elif had_public_identity:
            raise ConfigurationError(
                f"no SSH Agent key matches configured public identities: {key_paths}"
            )
        elif password:
            connect["password"] = password

        try:
            self._ssh.connect(**connect)
            self._sftp = self._ssh.open_sftp()
        except Exception as exc:
            self._ssh.close()
            raise GitDeployError(f"SFTP connection failed for {username}@{host}: {exc}") from exc
        self._directories: set[str] = set()
        # Learned once per connection: None = unknown, True/False = confirmed
        # by a prior posix_rename call on this session.
        self._posix_rename_supported: bool | None = None

    def read_file(
        self,
        remote_path: str,
        progress: ByteProgress | None = None,
    ) -> bytes | None:
        """Read one SFTP file without changing remote state.

        Args:
            remote_path: Absolute remote POSIX path.
            progress: Optional callback receiving each downloaded chunk size.

        Returns:
            File bytes, or ``None`` when absent.
        """

        try:
            chunks: list[bytes] = []
            with self._sftp.open(remote_path, "rb") as handle:
                while chunk := handle.read(TRANSFER_BLOCK_SIZE):
                    chunks.append(chunk)
                    if progress is not None:
                        progress(len(chunk))
            return b"".join(chunks)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                return None
            raise GitDeployError(f"cannot read remote file {remote_path}: {exc}") from exc

    def is_executable(self, remote_path: str) -> bool | None:
        """Inspect whether an SFTP file has any executable mode bit.

        Args:
            remote_path: Absolute remote POSIX path.

        Returns:
            Executable state, or ``None`` when the file is absent.
        """

        try:
            return bool(self._sftp.stat(remote_path).st_mode & 0o111)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                return None
            raise GitDeployError(f"cannot stat remote file {remote_path}: {exc}") from exc

    def replace_file(
        self,
        remote_path: str,
        data: bytes,
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Upload and replace one SFTP file through a temporary sibling.

        Args:
            remote_path: Absolute destination path.
            data: Target bytes.
            executable: Preserve an executable Git mode when true.
            progress: Optional callback receiving each uploaded chunk size.
        """

        self.replace_file_stream(
            remote_path,
            (data,),
            executable=executable,
            progress=progress,
        )

    def replace_file_stream(
        self,
        remote_path: str,
        chunks: Iterable[bytes],
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Stream chunks to SFTP temp, then chmod and atomic rename."""

        self._ensure_dir(posixpath.dirname(remote_path))
        temporary = remote_path + f".git-deploy-{os.getpid()}.tmp"
        try:
            with self._sftp.open(temporary, "wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
                    if progress is not None:
                        progress(len(chunk))
            mode = (
                self._permissions.executable_mode
                if executable
                else self._permissions.file_mode
            )
            self._sftp.chmod(temporary, mode)
            self._set_ownership(temporary)
            self._publish_temporary(temporary, remote_path)
        except Exception as exc:
            try:
                self._sftp.remove(temporary)
            except Exception:
                pass
            raise GitDeployError(f"cannot replace remote file {remote_path}: {exc}") from exc

    def _publish_temporary(self, temporary: str, remote_path: str) -> None:
        """Atomically publish a staged temporary file over ``remote_path``.

        Prefers the ``posix-rename@openssh.com`` extension. Any failure —
        extension genuinely unsupported, permission denied, a transient
        network/session error, or a generic server-side failure — falls back
        to a non-destructive two-step rename instead of guessing whether the
        specific error text means "unsupported". P1-06's safety property does
        not depend on classifying *why* posix_rename failed: the fallback
        itself never blindly deletes the live target. It swaps the current
        target to a sibling backup name (itself an atomic rename), publishes
        the new content, and restores the backup if the final rename fails —
        so it is safe to attempt even when the original failure was
        transient, and a server that lacks the extension but reports it with
        unpredictable wording still gets a working, crash-safe deploy instead
        of a permanent hard failure on every future write.

        Args:
            temporary: Already-written, chmod'd, owned temporary sibling path.
            remote_path: Final destination path.

        Returns:
            None.
        """

        if self._posix_rename_supported is not False:
            try:
                self._sftp.posix_rename(temporary, remote_path)
                self._posix_rename_supported = True
                return
            except (OSError, IOError):
                # Remember only that posix_rename itself doesn't work on this
                # connection (skip the redundant attempt next call); do not
                # try to classify *why* — see docstring above.
                self._posix_rename_supported = False

        backup = remote_path + f".git-deploy-{os.getpid()}.bak"
        try:
            self._sftp.stat(remote_path)
            target_existed = True
        except FileNotFoundError:
            target_existed = False
        if target_existed:
            self._sftp.rename(remote_path, backup)
        try:
            self._sftp.rename(temporary, remote_path)
        except Exception:
            if target_existed:
                try:
                    self._sftp.rename(backup, remote_path)
                except Exception:
                    pass
            raise
        if target_existed:
            try:
                self._sftp.remove(backup)
            except Exception:
                pass

    def list_files(self, remote_prefix: str) -> tuple[str, ...]:
        """Recursively list non-directory SFTP paths without following symlinks."""

        try:
            root_mode = self._sftp.lstat(remote_prefix).st_mode
        except FileNotFoundError:
            return ()
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                return ()
            raise GitDeployError(f"cannot list remote path {remote_prefix}: {exc}") from exc
        if not stat.S_ISDIR(root_mode):
            return (remote_prefix,)
        found: list[str] = []
        pending = [remote_prefix]
        while pending:
            current = pending.pop()
            try:
                attributes = self._sftp.listdir_attr(current)
            except OSError as exc:
                raise GitDeployError(f"cannot list remote directory {current}: {exc}") from exc
            for item in attributes:
                path = posixpath.join(current, item.filename)
                if stat.S_ISDIR(item.st_mode):
                    pending.append(path)
                else:
                    found.append(path)
        return tuple(sorted(found))

    def delete_file(self, remote_path: str) -> None:
        """Delete one SFTP file when it exists.

        Args:
            remote_path: Absolute remote path.
        """

        try:
            self._sftp.remove(remote_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            if getattr(exc, "errno", None) != 2:
                raise GitDeployError(f"cannot delete remote file {remote_path}: {exc}") from exc

    def execute(self, command: str) -> tuple[int, str, str]:
        """Execute one command through the established SSH session.

        Args:
            command: Trusted static command from deployment configuration.

        Returns:
            Exit code, decoded stdout, and decoded stderr.
        """

        _, stdout, stderr = self._ssh.exec_command(command)
        code = stdout.channel.recv_exit_status()
        return (
            code,
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
        )

    def close(self) -> None:
        """Close SFTP, SSH, and retained SSH Agent resources."""

        self._sftp.close()
        self._ssh.close()
        if self._agent is not None:
            self._agent.close()

    def _ensure_dir(self, remote_dir: str) -> None:
        """Create one remote directory tree idempotently.

        Args:
            remote_dir: Absolute remote directory.
        """

        if remote_dir in self._directories or remote_dir in {"", "/"}:
            return
        parent = posixpath.dirname(remote_dir.rstrip("/"))
        self._ensure_dir(parent)
        try:
            self._sftp.stat(remote_dir)
        except FileNotFoundError:
            try:
                self._sftp.mkdir(remote_dir)
                self._sftp.chmod(remote_dir, self._permissions.directory_mode)
                self._set_ownership(remote_dir)
            except OSError as exc:
                raise GitDeployError(
                    f"cannot prepare remote directory {remote_dir}: {exc}"
                ) from exc
        self._directories.add(remote_dir)

    def _set_ownership(self, remote_path: str) -> None:
        """Apply configured owner/group before publishing a remote path.

        Args:
            remote_path: Absolute SFTP path to update.

        Raises:
            GitDeployError: If the SSH account cannot apply configured ownership.
        """

        owner = self._permissions.owner
        group = self._permissions.group
        if owner is None and group is None:
            return
        quoted_path = shlex.quote(remote_path)
        if owner is None:
            command = f"chgrp {shlex.quote(group or '')} {quoted_path}"
        else:
            ownership = owner if group is None else f"{owner}:{group}"
            command = f"chown {shlex.quote(ownership)} {quoted_path}"
        code, _, stderr = self.execute(command)
        if code != 0:
            detail = stderr.strip() or f"exit status {code}"
            raise GitDeployError(
                f"cannot set remote ownership for {remote_path}: {detail}"
            )


def ftps_tls_trust_digest(server: dict[str, Any]) -> str:
    """Return a secret-free digest of FTPS trust-boundary settings.

    Includes verification mode, CA file path, and client cert path presence —
    never private key material or passwords. Used for identity/policy summaries.

    Args:
        server: Effective FTPS connection mapping.

    Returns:
        Lowercase hex SHA-256 digest of the trust configuration.
    """

    import json

    verify = _ftps_tls_verify_enabled(server)
    ca_file = str(server.get("tls_ca_file") or server.get("ca_file") or "").strip()
    cert_file = str(server.get("tls_cert_file") or server.get("cert_file") or "").strip()
    payload = {
        "schema_version": 1,
        "tls_ca_file": ca_file,
        "tls_cert_file": cert_file,
        "tls_verify": verify,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ftps_tls_verify_enabled(server: dict[str, Any]) -> bool:
    """Return whether FTPS must verify server certificates (default true).

    Args:
        server: Effective FTPS connection mapping.

    Returns:
        True when verification is required.
    """

    if "tls_verify" in server:
        return bool(server.get("tls_verify"))
    if "ssl_verify" in server:
        return bool(server.get("ssl_verify"))
    # Default fail-closed: only explicit false opts out.
    return True


def build_ftps_ssl_context(server: dict[str, Any]) -> Any:
    """Build an SSLContext for FTPS with verified defaults.

    Default: CERT_REQUIRED + hostname checking, system/default CA trust, optional
    custom CA and client certificate. Explicit ``tls_verify=false`` creates an
    unverified context and must only be used for legacy servers.

    Args:
        server: Effective FTPS connection mapping.

    Returns:
        Configured ``ssl.SSLContext`` for ``ftplib.FTP_TLS``.

    Raises:
        ConfigurationError: P1-08: a missing CA/cert/key file, a corrupt PEM,
            or a cert/key mismatch is reported as a structured configuration
            error instead of a raw ``ssl.SSLError``/``OSError``.
    """

    import ssl
    import warnings

    verify = _ftps_tls_verify_enabled(server)
    ca_file = str(server.get("tls_ca_file") or server.get("ca_file") or "").strip() or None
    cert_file = str(server.get("tls_cert_file") or server.get("cert_file") or "").strip() or None
    key_file = str(server.get("tls_key_file") or server.get("key_file") or "").strip() or None

    try:
        if not verify:
            warnings.warn(
                "FTPS tls_verify=false disables server certificate and hostname "
                "verification; credentials and payloads are exposed to MITM",
                UserWarning,
                stacklevel=2,
            )
            context = ssl._create_unverified_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            if cert_file:
                context.load_cert_chain(cert_file, key_file)
            return context

        context = ssl.create_default_context(cafile=ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        if cert_file:
            context.load_cert_chain(cert_file, key_file)
        return context
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise ConfigurationError(
            f"invalid FTPS TLS configuration (ca_file={ca_file!r} "
            f"cert_file={cert_file!r} key_file={key_file!r}): {exc}"
        ) from exc


class FtpTransport:
    """FTP/FTPS transport implementing the same backup-friendly file API."""

    supports_commands = False

    def __init__(self, server: dict[str, Any], use_tls: bool):
        """Connect and authenticate an FTP or explicit FTPS session.

        Args:
            server: Effective FTP connection mapping.
            use_tls: Enable TLS for control and data channels.
        """

        from .target_identity import effective_port

        host = str(server.get("host", "")).strip()
        username = str(server.get("username", "")).strip()
        if not host or not username:
            raise ConfigurationError("FTP requires host and username")
        # Identity and connect must share the same default-port source of truth.
        protocol = "ftps" if use_tls else "ftp"
        connect_port = effective_port(protocol, server.get("port"))
        if use_tls:
            context = build_ftps_ssl_context(server)
            # FTP_TLS(context=...) is supported on CPython 3.9+.
            self._ftp = ftplib.FTP_TLS(context=context)
            self._tls_trust_digest = ftps_tls_trust_digest(server)
        else:
            self._ftp = ftplib.FTP()
            self._tls_trust_digest = ""
        try:
            self._ftp.connect(
                host,
                connect_port,
                timeout=int(server.get("timeout", 15)),
            )
            self._ftp.login(username, _resolve_password(server))
            if isinstance(self._ftp, ftplib.FTP_TLS):
                self._ftp.prot_p()
            self._ftp.set_pasv(bool(server.get("passive", True)))
        except Exception as exc:
            self._ftp.close()
            raise GitDeployError(f"FTP connection failed for {username}@{host}: {exc}") from exc
        self._directories: set[str] = set()

    def read_file(
        self,
        remote_path: str,
        progress: ByteProgress | None = None,
    ) -> bytes | None:
        """Download one FTP file into memory.

        Args:
            remote_path: Remote FTP path.
            progress: Optional callback receiving each downloaded chunk size.

        Returns:
            File bytes, or ``None`` for a 550 not-found response.
        """

        output = io.BytesIO()

        def consume(chunk: bytes) -> None:
            """Store a downloaded FTP chunk and report its size.

            Args:
                chunk: Downloaded data block.
            """

            output.write(chunk)
            if progress is not None:
                progress(len(chunk))

        try:
            self._ftp.retrbinary(
                f"RETR {remote_path}",
                consume,
                blocksize=TRANSFER_BLOCK_SIZE,
            )
        except ftplib.error_perm as exc:
            if str(exc).startswith("550"):
                return None
            raise GitDeployError(f"cannot read remote file {remote_path}: {exc}") from exc
        return output.getvalue()

    def is_executable(self, remote_path: str) -> bool | None:
        """Report that FTP cannot portably inspect executable mode bits.

        Args:
            remote_path: Remote FTP path, unused.

        Returns:
            Always ``None``.
        """

        del remote_path
        return None

    def replace_file(
        self,
        remote_path: str,
        data: bytes,
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Upload and rename one FTP file.

        Args:
            remote_path: Destination FTP path.
            data: Target bytes.
            executable: Ignored because standard FTP has no portable chmod.
            progress: Optional callback receiving each uploaded chunk size.
        """

        self.replace_file_stream(
            remote_path,
            (data,),
            executable=executable,
            progress=progress,
        )

    def replace_file_stream(
        self,
        remote_path: str,
        chunks: Iterable[bytes],
        executable: bool = False,
        progress: ByteProgress | None = None,
    ) -> None:
        """Stream iterable chunks through FTP STOR, then publish via a safe rename swap."""

        del executable
        self._ensure_dir(posixpath.dirname(remote_path))
        temporary = remote_path + f".git-deploy-{os.getpid()}.tmp"
        try:
            callback = (lambda chunk: progress(len(chunk))) if progress is not None else None
            self._ftp.storbinary(
                f"STOR {temporary}",
                _IterableReader(chunks),
                blocksize=TRANSFER_BLOCK_SIZE,
                callback=callback,
            )
            self._publish_temporary(temporary, remote_path)
        except Exception as exc:
            try:
                self._ftp.delete(temporary)
            except Exception:
                pass
            raise GitDeployError(f"cannot replace remote file {remote_path}: {exc}") from exc

    def _publish_temporary(self, temporary: str, remote_path: str) -> None:
        """Publish a staged temporary file over ``remote_path`` without ever
        blindly deleting the live target first.

        P1-06 also applies to plain FTP/FTPS: standard FTP has no atomic
        rename-over-existing-file guarantee, so this swaps the current target
        to a sibling backup name first (rather than deleting it outright),
        publishes the new content, and restores the backup if the final
        rename fails — the same non-destructive protocol used for SFTP.

        Args:
            temporary: Already-uploaded temporary sibling path.
            remote_path: Final destination path.

        Returns:
            None.
        """

        backup = remote_path + f".git-deploy-{os.getpid()}.bak"
        try:
            target_existed = self._ftp.size(remote_path) is not None
        except ftplib.all_errors:
            target_existed = False
        if target_existed:
            self._ftp.rename(remote_path, backup)
        try:
            self._ftp.rename(temporary, remote_path)
        except Exception:
            if target_existed:
                try:
                    self._ftp.rename(backup, remote_path)
                except Exception:
                    pass
            raise
        if target_existed:
            try:
                self._ftp.delete(backup)
            except Exception:
                pass

    def list_files(self, remote_prefix: str) -> tuple[str, ...]:
        """Recursively list FTP paths using MLSD, treating a direct file as present."""

        try:
            size = self._ftp.size(remote_prefix)
            if size is not None:
                return (remote_prefix,)
        except ftplib.all_errors:
            pass
        found: list[str] = []
        pending = [remote_prefix]
        while pending:
            current = pending.pop()
            try:
                rows = list(self._ftp.mlsd(current))
            except ftplib.error_perm as exc:
                if str(exc).startswith("550"):
                    if current == remote_prefix:
                        return ()
                    continue
                raise GitDeployError(f"cannot list FTP directory {current}: {exc}") from exc
            except (AttributeError, ftplib.error_proto) as exc:
                raise GitDeployError("FTP server must support MLSD for artifact tree baseline") from exc
            for name, facts in rows:
                if name in {".", ".."}:
                    continue
                path = posixpath.join(current, name)
                if facts.get("type") in {"dir", "cdir", "pdir"}:
                    if facts.get("type") == "dir":
                        pending.append(path)
                else:
                    found.append(path)
        return tuple(sorted(found))

    def delete_file(self, remote_path: str) -> None:
        """Delete one FTP file when present.

        Args:
            remote_path: Remote FTP path.
        """

        try:
            self._ftp.delete(remote_path)
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise GitDeployError(f"cannot delete remote file {remote_path}: {exc}") from exc

    def execute(self, command: str) -> tuple[int, str, str]:
        """Report that FTP cannot execute remote shell commands.

        Args:
            command: Configured command that cannot be run over FTP.

        Returns:
            Stable unsupported-operation result.
        """

        return 1, "", f"FTP cannot execute remote command: {command}"

    def close(self) -> None:
        """Close the FTP connection."""

        try:
            self._ftp.quit()
        except Exception:
            self._ftp.close()

    def _ensure_dir(self, remote_dir: str) -> None:
        """Create one FTP directory tree idempotently.

        Args:
            remote_dir: Remote directory path.
        """

        if remote_dir in self._directories or remote_dir in {"", "/"}:
            return
        parent = posixpath.dirname(remote_dir.rstrip("/"))
        self._ensure_dir(parent)
        try:
            self._ftp.mkd(remote_dir)
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise GitDeployError(f"cannot create remote directory {remote_dir}: {exc}") from exc
        self._directories.add(remote_dir)


def _resolve_password(server: dict[str, Any]) -> str:
    """Resolve a password from an environment variable or explicit value.

    Args:
        server: Effective server configuration.

    Returns:
        Password or private-key passphrase, possibly empty.
    """

    variable = str(server.get("password_env", "")).strip()
    if variable:
        value = os.environ.get(variable)
        if value is None:
            raise ConfigurationError(f"password environment variable is not set: {variable}")
        return value
    return str(server.get("password", ""))


class _IterableReader:
    """Expose an iterable of bounded chunks as the ``read`` API used by ftplib."""

    def __init__(self, chunks: Iterable[bytes]):
        """Bind a single-pass chunk iterator."""

        self._chunks: Iterator[bytes] = iter(chunks)
        self._buffer = bytearray()
        self._done = False

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes without aggregating the complete payload."""

        if size < 0:
            size = TRANSFER_BLOCK_SIZE
        while len(self._buffer) < size and not self._done:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._done = True
                break
            self._buffer.extend(chunk)
        output = bytes(self._buffer[:size])
        del self._buffer[:size]
        return output


def _is_public_key(path: Path) -> bool:
    """Return whether a readable file uses an OpenSSH public-key prefix.

    Args:
        path: Candidate identity path.

    Returns:
        ``True`` for a recognized public-key line.
    """

    if not path.is_file():
        return False
    try:
        prefix = path.read_text(encoding="utf-8", errors="ignore")[:32].strip()
    except OSError:
        return False
    return prefix.startswith(("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-"))


def _agent_key_for_public_file(path: Path) -> tuple[Any | None, Any | None]:
    """Find exactly one SSH Agent key matching an OpenSSH public key file.

    Args:
        path: Public identity file exported for the managed private key.

    Returns:
        Matched Paramiko AgentKey and retained Agent connection, or two ``None`` values.
    """

    try:
        import paramiko
    except ModuleNotFoundError as exc:
        raise ConfigurationError("SFTP requires the paramiko package") from exc
    fields = path.read_text(encoding="utf-8").split()
    if len(fields) < 2:
        return None, None
    try:
        target_blob = base64.b64decode(fields[1], validate=True)
    except ValueError:
        return None, None
    target_fingerprint = hashlib.md5(target_blob).digest()  # noqa: S324 - OpenSSH identity comparison only.
    agent = paramiko.Agent()
    for key in agent.get_keys():
        if key.get_fingerprint() == target_fingerprint:
            return key, agent
    agent.close()
    return None, None
