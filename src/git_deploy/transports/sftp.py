"""Paramiko SFTP adapter with SSH config/agent and safe temporary replacement."""

from __future__ import annotations

import errno
import os
import posixpath
import uuid
from pathlib import Path, PurePosixPath

import paramiko

from git_deploy.config import TargetConfig, resolve_ssh_target
from git_deploy.errors import ConfigError, DeployError
from git_deploy.transports.base import ProgressCallback, Transport


class SFTPTransport(Transport):
    """Upload and delete files below one configured SFTP root."""

    def __init__(self, target: TargetConfig) -> None:
        """Create an unconnected SFTP adapter.

        Args:
            target: Validated SFTP target settings.
        """

        self.target = target
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None

    def connect(self) -> None:
        """Resolve SSH config, verify host identity, and authenticate."""

        settings = _resolve_ssh_settings(self.target)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.target.known_hosts_file is not None:
            try:
                client.load_host_keys(str(self.target.known_hosts_file))
            except OSError as exc:
                raise DeployError(
                    f"cannot load known hosts file {self.target.known_hosts_file}: {exc}"
                ) from exc
        if self.target.strict_host_key_checking:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        password = _password(self.target)
        try:
            client.connect(
                hostname=settings["host"],
                port=settings["port"],
                username=settings["username"],
                password=password,
                key_filename=settings["key_files"] or None,
                timeout=self.target.timeout,
                banner_timeout=self.target.timeout,
                auth_timeout=self.target.timeout,
                allow_agent=self.target.use_ssh_agent,
                look_for_keys=False,
            )
            self.sftp = client.open_sftp()
            self.client = client
        except Exception as exc:
            client.close()
            raise DeployError(f"SFTP connection failed for target {self.target.name}: {exc}") from exc

    def ensure_root(self) -> None:
        """Create the absolute remote root recursively when missing."""

        self._mkdirs(self.target.remote_root.as_posix())

    def root_exists(self) -> bool:
        """Return whether the configured SFTP root exists."""

        try:
            self._require_sftp().stat(self.target.remote_root.as_posix())
            return True
        except OSError as exc:
            if getattr(exc, "errno", None) in {errno.ENOENT, 2}:
                return False
            raise DeployError(f"cannot inspect SFTP root {self.target.remote_root}: {exc}") from exc

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Upload to a temporary name and safely replace the destination.

        Args:
            local_path: Frozen local file to stream.
            remote_path: Normalized relative target path.
            callback: Byte progress callback.
            executable: Publish committed executables as ``0755``; other files as ``0644``.
        """

        sftp = self._require_sftp()
        target = self._absolute(remote_path)
        self._mkdirs(posixpath.dirname(target))
        temporary = f"{target}.git-deploy-{uuid.uuid4().hex}.tmp"
        try:
            sftp.put(str(local_path), temporary, callback=callback, confirm=True)
            sftp.chmod(temporary, 0o755 if executable else 0o644)
            self._publish_temporary(temporary, target)
        except Exception as exc:
            try:
                sftp.remove(temporary)
            except Exception:
                pass
            if isinstance(exc, DeployError):
                raise
            raise DeployError(f"SFTP upload failed for {remote_path}: {exc}") from exc

    def delete(self, remote_path: str) -> None:
        """Delete a remote file while treating absence as success.

        Args:
            remote_path: Normalized relative target path.
        """

        try:
            self._require_sftp().remove(self._absolute(remote_path))
        except OSError as exc:
            if getattr(exc, "errno", None) in {errno.ENOENT, 2}:
                return
            raise DeployError(f"SFTP delete failed for {remote_path}: {exc}") from exc

    def close(self) -> None:
        """Close SFTP then SSH resources, tolerating partial connection."""

        try:
            if self.sftp is not None:
                self.sftp.close()
        finally:
            self.sftp = None
            if self.client is not None:
                self.client.close()
                self.client = None

    def _publish_temporary(self, temporary: str, target: str) -> None:
        """Prefer POSIX overwrite; otherwise use a recoverable backup swap."""

        sftp = self._require_sftp()
        try:
            sftp.posix_rename(temporary, target)
            return
        except OSError:
            pass
        backup = f"{target}.git-deploy-{uuid.uuid4().hex}.bak"
        had_target = False
        try:
            sftp.rename(target, backup)
            had_target = True
        except OSError as exc:
            if getattr(exc, "errno", None) not in {errno.ENOENT, 2}:
                raise DeployError(f"cannot preserve existing SFTP target {target}: {exc}") from exc
        try:
            sftp.rename(temporary, target)
        except OSError as exc:
            if had_target:
                try:
                    sftp.rename(backup, target)
                except OSError as restore_exc:
                    raise DeployError(
                        f"SFTP publish and backup restore both failed for {target}: {restore_exc}"
                    ) from exc
            raise DeployError(f"SFTP publish failed for {target}: {exc}") from exc
        if had_target:
            try:
                sftp.remove(backup)
            except OSError:
                pass

    def _mkdirs(self, absolute: str) -> None:
        """Create every missing directory component without changing existing ones."""

        sftp = self._require_sftp()
        current = "/"
        for component in PurePosixPath(absolute).parts[1:]:
            current = posixpath.join(current, component)
            try:
                sftp.stat(current)
            except OSError as exc:
                if getattr(exc, "errno", None) not in {errno.ENOENT, 2}:
                    raise DeployError(f"cannot inspect SFTP directory {current}: {exc}") from exc
                try:
                    sftp.mkdir(current)
                except OSError as mkdir_exc:
                    raise DeployError(f"cannot create SFTP directory {current}: {mkdir_exc}") from mkdir_exc

    def _absolute(self, relative: str) -> str:
        """Join a normalized relative path below the configured root."""

        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise DeployError(f"unsafe SFTP relative path: {relative!r}")
        return (self.target.remote_root / path).as_posix()

    def _require_sftp(self) -> paramiko.SFTPClient:
        """Return the active SFTP session or fail clearly."""

        if self.sftp is None:
            raise DeployError("SFTP transport is not connected")
        return self.sftp


def _resolve_ssh_settings(target: TargetConfig) -> dict[str, object]:
    """Resolve host/user/port/identity files using OpenSSH's own parser."""

    try:
        resolved = resolve_ssh_target(target)
    except ConfigError as exc:
        raise DeployError(str(exc)) from exc
    return {
        "host": resolved.host,
        "username": resolved.username,
        "port": resolved.port,
        "key_files": list(resolved.key_files),
    }


def _password(target: TargetConfig) -> str | None:
    """Resolve an optional password/passphrase without persisting its value."""

    if not target.password_env:
        return None
    value = os.environ.get(target.password_env)
    if value is None:
        raise DeployError(f"required password environment variable is not set: {target.password_env}")
    return value
