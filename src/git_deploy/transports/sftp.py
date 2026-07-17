"""Paramiko SFTP adapter with SSH config/agent and safe temporary replacement."""

from __future__ import annotations

import errno
import os
import posixpath
import shlex
import stat
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import TextIO

import paramiko

from git_deploy.config import TargetConfig, resolve_ssh_target
from git_deploy.errors import ConfigError, DeployError
from git_deploy.transports.base import ProgressCallback, RemotePathType, Transport


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

    def run_command(
        self,
        command: str,
        *,
        cwd: PurePosixPath,
        timeout: float | None,
    ) -> None:
        """Run one non-interactive SSH command and stream both output channels.

        Args:
            command: Validated one-line shell command.
            cwd: Absolute remote working directory.
            timeout: Optional whole-command timeout in seconds.

        Returns:
            ``None`` after a zero remote exit status.
        """

        client = self._require_client()
        wrapped = f"cd -- {shlex.quote(cwd.as_posix())} && {command}"
        stdin = stdout = stderr = channel = None
        try:
            stdin, stdout, stderr = client.exec_command(
                wrapped,
                timeout=timeout,
                get_pty=False,
            )
            channel = stdout.channel
            stdin.close()
            status = _stream_command_channel(channel, timeout)
        except DeployError:
            raise
        except Exception as exc:
            raise DeployError(f"remote command execution failed: {exc}") from exc
        finally:
            if stdin is not None:
                stdin.close()
            for stream in (stdout, stderr):
                if stream is not None:
                    stream.close()
            if channel is not None:
                channel.close()
        if status != 0:
            raise DeployError(f"remote command failed with exit={status}: {command}")

    def lstat(self, remote_path: str) -> RemotePathType:
        """Classify one relative path without following a remote symlink.

        Args:
            remote_path: Safe relative path below ``remote_root``.

        Returns:
            Confirmed remote path type or absence.
        """

        try:
            mode = self._require_sftp().lstat(self._absolute(remote_path)).st_mode
        except OSError as exc:
            if getattr(exc, "errno", None) in {errno.ENOENT, 2}:
                return RemotePathType.MISSING
            raise DeployError(f"cannot inspect remote path {remote_path}: {exc}") from exc
        if mode is None:
            return RemotePathType.OTHER
        if stat.S_ISLNK(mode):
            return RemotePathType.SYMLINK
        if stat.S_ISREG(mode):
            return RemotePathType.FILE
        if stat.S_ISDIR(mode):
            return RemotePathType.DIRECTORY
        return RemotePathType.OTHER

    def read_file(self, remote_path: str, *, max_bytes: int) -> bytes:
        """Read one bounded regular remote file without following symlinks.

        Args:
            remote_path: Safe relative metadata path.
            max_bytes: Maximum accepted byte count.

        Returns:
            Exact remote bytes.
        """

        if self.lstat(remote_path) is not RemotePathType.FILE:
            raise DeployError(f"remote metadata path is not a regular file: {remote_path}")
        absolute = self._absolute(remote_path)
        try:
            attributes = self._require_sftp().lstat(absolute)
            if attributes.st_size is None or attributes.st_size > max_bytes:
                raise DeployError(f"remote metadata file exceeds {max_bytes} bytes: {remote_path}")
            with self._require_sftp().open(absolute, "rb") as handle:
                data = handle.read(max_bytes + 1)
        except DeployError:
            raise
        except OSError as exc:
            raise DeployError(f"cannot read remote metadata {remote_path}: {exc}") from exc
        if len(data) > max_bytes:
            raise DeployError(f"remote metadata file exceeds {max_bytes} bytes: {remote_path}")
        return bytes(data)

    def write_file_atomic(self, remote_path: str, data: bytes) -> None:
        """Publish small metadata bytes through a chmodded temporary file."""

        sftp = self._require_sftp()
        target = self._absolute(remote_path)
        self._mkdirs(posixpath.dirname(target))
        temporary = f"{target}.git-deploy-{uuid.uuid4().hex}.tmp"
        try:
            with sftp.open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
            sftp.chmod(temporary, 0o644)
            self._publish_temporary(temporary, target)
        except Exception as exc:
            try:
                sftp.remove(temporary)
            except Exception:
                pass
            if isinstance(exc, DeployError):
                raise
            raise DeployError(f"cannot publish remote metadata {remote_path}: {exc}") from exc

    def list_directory(self, remote_path: str) -> tuple[str, ...]:
        """List direct child names for one verified remote directory."""

        path_type = self.lstat(remote_path)
        if path_type is RemotePathType.MISSING:
            return ()
        if path_type is not RemotePathType.DIRECTORY:
            raise DeployError(f"remote path is not a directory: {remote_path}")
        try:
            return tuple(sorted(self._require_sftp().listdir(self._absolute(remote_path))))
        except OSError as exc:
            raise DeployError(f"cannot list remote directory {remote_path}: {exc}") from exc

    def make_directory(self, remote_path: str, *, mode: int = 0o755) -> None:
        """Create a safe relative remote directory recursively."""

        absolute = self._absolute(remote_path)
        self._mkdirs(absolute, mode=mode)
        try:
            self._require_sftp().chmod(absolute, mode)
        except OSError as exc:
            raise DeployError(f"cannot chmod remote directory {remote_path}: {exc}") from exc

    def rename_path(self, source: str, destination: str) -> None:
        """Rename one remote path while refusing implicit destination overwrite."""

        if self.lstat(destination) is not RemotePathType.MISSING:
            raise DeployError(f"remote rename destination already exists: {destination}")
        try:
            self._require_sftp().rename(self._absolute(source), self._absolute(destination))
        except OSError as exc:
            raise DeployError(f"cannot rename remote path {source} to {destination}: {exc}") from exc

    def remove_tree(self, remote_path: str) -> None:
        """Remove one remote tree recursively without following symlinks."""

        kind = self.lstat(remote_path)
        if kind is RemotePathType.MISSING:
            return
        absolute = self._absolute(remote_path)
        try:
            if kind is RemotePathType.DIRECTORY:
                for name in self.list_directory(remote_path):
                    self.remove_tree((PurePosixPath(remote_path) / name).as_posix())
                self._require_sftp().rmdir(absolute)
            elif kind in {RemotePathType.FILE, RemotePathType.SYMLINK}:
                self._require_sftp().remove(absolute)
            else:
                raise DeployError(f"refusing to remove unsupported remote type: {remote_path}")
        except DeployError:
            raise
        except OSError as exc:
            raise DeployError(f"cannot remove remote tree {remote_path}: {exc}") from exc

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

    def _mkdirs(self, absolute: str, *, mode: int = 0o755) -> None:
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
                    sftp.mkdir(current, mode=mode)
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

    def _require_client(self) -> paramiko.SSHClient:
        """Return the active SSH client or fail clearly."""

        if self.client is None:
            raise DeployError("SFTP transport is not connected")
        return self.client


def _stream_command_channel(channel: paramiko.Channel, timeout: float | None) -> int:
    """Drain Paramiko stdout/stderr live and return the remote exit status.

    Args:
        channel: Exec channel shared by stdout and stderr file objects.
        timeout: Optional whole-command timeout in seconds.

    Returns:
        Remote process exit status after both output streams are drained.
    """

    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        progressed = False
        if channel.recv_ready():
            data = channel.recv(64 * 1024)
            if data:
                _write_command_output(sys.stdout, data)
                progressed = True
        if channel.recv_stderr_ready():
            data = channel.recv_stderr(64 * 1024)
            if data:
                _write_command_output(sys.stderr, data)
                progressed = True
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            return channel.recv_exit_status()
        if deadline is not None and time.monotonic() >= deadline:
            channel.close()
            raise DeployError(f"remote command timed out after {timeout} second(s)")
        if not progressed:
            time.sleep(0.01)


def _write_command_output(stream: TextIO, data: bytes) -> None:
    """Write remote bytes immediately without assuming a UTF-8 chunk boundary.

    Args:
        stream: Current stdout/stderr text stream.
        data: Raw remote output bytes.

    Returns:
        ``None`` after flushing the current terminal stream.
    """

    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
        return
    stream.write(data.decode("utf-8", errors="replace"))
    stream.flush()


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
