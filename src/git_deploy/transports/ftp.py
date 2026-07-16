"""Binary passive/active FTP adapter with idempotent file operations."""

from __future__ import annotations

import ftplib
import os
from pathlib import Path, PurePosixPath

from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError
from git_deploy.transports.base import ProgressCallback, Transport


class FTPTransport(Transport):
    """Upload and delete files below one configured FTP root."""

    def __init__(self, target: TargetConfig) -> None:
        """Create an unconnected FTP adapter.

        Args:
            target: Validated FTP target settings.
        """

        self.target = target
        self.ftp: ftplib.FTP | None = None

    def connect(self) -> None:
        """Connect and authenticate with a password from the environment."""

        if not self.target.password_env:
            raise DeployError("FTP target is missing password_env")
        password = os.environ.get(self.target.password_env)
        if password is None:
            raise DeployError(f"required password environment variable is not set: {self.target.password_env}")
        ftp = ftplib.FTP()
        try:
            ftp.connect(self.target.host or "", self.target.port, timeout=self.target.timeout)
            ftp.login(self.target.username or "", password)
            ftp.set_pasv(self.target.passive)
            self.ftp = ftp
        except Exception as exc:
            try:
                ftp.close()
            except Exception:
                pass
            raise DeployError(f"FTP connection failed for target {self.target.name}: {exc}") from exc

    def ensure_root(self) -> None:
        """Create the absolute configured FTP root recursively when missing."""

        self._mkdirs(self.target.remote_root.as_posix())

    def upload(self, local_path: Path, remote_path: str, callback: ProgressCallback) -> None:
        """Stream one file in binary mode after creating its parent directories.

        Args:
            local_path: Frozen local file to stream.
            remote_path: Normalized relative target path.
            callback: Byte progress callback.
        """

        ftp = self._require_ftp()
        target = self._absolute(remote_path)
        self._mkdirs(PurePosixPath(target).parent.as_posix())
        total = local_path.stat().st_size
        transferred = 0

        def block_callback(block: bytes) -> None:
            """Adapt ftplib's block callback to the shared byte callback."""

            nonlocal transferred
            transferred += len(block)
            callback(transferred, total)

        try:
            with local_path.open("rb") as handle:
                ftp.storbinary(f"STOR {target}", handle, blocksize=64 * 1024, callback=block_callback)
            if total == 0:
                callback(0, 0)
        except (OSError, ftplib.Error) as exc:
            raise DeployError(f"FTP upload failed for {remote_path}: {exc}") from exc

    def delete(self, remote_path: str) -> None:
        """Delete one file; only explicit not-found replies count as success.

        Args:
            remote_path: Normalized relative target path.
        """

        try:
            self._require_ftp().delete(self._absolute(remote_path))
        except ftplib.error_perm as exc:
            message = str(exc).lower()
            if any(marker in message for marker in ("not found", "no such file", "does not exist")):
                return
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc
        except ftplib.Error as exc:
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc

    def close(self) -> None:
        """Quit cleanly when possible, otherwise close the socket."""

        if self.ftp is None:
            return
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass
        finally:
            self.ftp = None

    def _mkdirs(self, absolute: str) -> None:
        """Create missing FTP directories while preserving existing ones."""

        ftp = self._require_ftp()
        current = "/"
        for component in PurePosixPath(absolute).parts[1:]:
            current = (PurePosixPath(current) / component).as_posix()
            try:
                ftp.mkd(current)
            except ftplib.error_perm as exc:
                if not str(exc).startswith("550"):
                    raise DeployError(f"cannot create FTP directory {current}: {exc}") from exc
                try:
                    original = ftp.pwd()
                    ftp.cwd(current)
                    ftp.cwd(original)
                except ftplib.Error as inspect_exc:
                    raise DeployError(f"FTP directory is unavailable {current}: {inspect_exc}") from inspect_exc

    def _absolute(self, relative: str) -> str:
        """Join a normalized relative path below the configured root."""

        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise DeployError(f"unsafe FTP relative path: {relative!r}")
        return (self.target.remote_root / path).as_posix()

    def _require_ftp(self) -> ftplib.FTP:
        """Return the active FTP session or fail clearly."""

        if self.ftp is None:
            raise DeployError("FTP transport is not connected")
        return self.ftp
