"""Binary passive/active FTP adapter with idempotent file operations."""

from __future__ import annotations

import ftplib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError
from git_deploy.transports.base import ProgressCallback, Transport


class FTPPathProbeResult(Enum):
    """Classify an FTP directory probe without collapsing access errors."""

    EXISTS = "exists"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FTPDirectoryProbe:
    """Carry one directory probe result, cached names, and optional error detail."""

    result: FTPPathProbeResult
    entries: frozenset[str] = frozenset()
    error: str | None = None


class FTPTransport(Transport):
    """Upload and delete files below one configured FTP root."""

    def __init__(self, target: TargetConfig) -> None:
        """Create an unconnected FTP adapter.

        Args:
            target: Validated FTP target settings.
        """

        self.target = target
        self.ftp: ftplib.FTP | None = None
        self._directory_entries: dict[str, set[str]] = {}

    def connect(self) -> None:
        """Connect and authenticate with a password from the environment."""

        self._directory_entries.clear()
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

    def root_exists(self) -> bool:
        """Check the configured FTP root without creating it."""

        ftp = self._require_ftp()
        try:
            original = ftp.pwd()
            ftp.cwd(self.target.remote_root.as_posix())
            ftp.cwd(original)
            return True
        except ftplib.error_perm as exc:
            if str(exc).startswith("550"):
                return False
            raise DeployError(f"cannot inspect FTP root {self.target.remote_root}: {exc}") from exc
        except ftplib.Error as exc:
            raise DeployError(f"cannot inspect FTP root {self.target.remote_root}: {exc}") from exc

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Stream one file in binary mode after creating its parent directories.

        Args:
            local_path: Frozen local file to stream.
            remote_path: Normalized relative target path.
            callback: Byte progress callback.
            executable: Unsupported POSIX executable-mode request.
        """

        if executable:
            raise DeployError(f"FTP cannot guarantee executable mode for {remote_path}")

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
            self._cache_add(PurePosixPath(target).parent.as_posix(), PurePosixPath(target).name)
        except (OSError, ftplib.Error) as exc:
            raise DeployError(f"FTP upload failed for {remote_path}: {exc}") from exc

    def delete(self, remote_path: str) -> None:
        """Delete one file after a language-independent parent listing probe.

        Args:
            remote_path: Normalized relative target path.

        Returns:
            ``None`` after confirmed absence or successful deletion.
        """

        ftp = self._require_ftp()
        target = self._absolute(remote_path)
        parent = PurePosixPath(target).parent.as_posix()
        name = PurePosixPath(target).name
        probe = self._probe_directory(parent)
        if probe.result is FTPPathProbeResult.MISSING:
            return
        if probe.result is FTPPathProbeResult.ERROR:
            raise DeployError(
                f"FTP existence probe failed for {remote_path}: {probe.error or 'unknown error'}"
            )
        if name not in probe.entries:
            return
        try:
            ftp.delete(target)
        except ftplib.error_perm as exc:
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc
        except ftplib.Error as exc:
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc
        self._cache_discard(parent, name)

    def close(self) -> None:
        """Quit cleanly when possible, otherwise close the socket."""

        self._directory_entries.clear()
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
                parent = PurePosixPath(current).parent.as_posix()
                self._cache_add(parent, PurePosixPath(current).name)
                self._directory_entries[current] = set()
            except ftplib.error_perm as exc:
                if not str(exc).startswith("550"):
                    raise DeployError(f"cannot create FTP directory {current}: {exc}") from exc
                try:
                    original = ftp.pwd()
                    ftp.cwd(current)
                    ftp.cwd(original)
                except ftplib.Error as inspect_exc:
                    raise DeployError(f"FTP directory is unavailable {current}: {inspect_exc}") from inspect_exc

    def _probe_directory(self, absolute: str) -> FTPDirectoryProbe:
        """List one directory once and distinguish missing parents from access errors.

        Args:
            absolute: Absolute POSIX directory path below or containing the target root.

        Returns:
            Explicit probe state with normalized child names when the directory exists.
        """

        cached = self._directory_entries.get(absolute)
        if cached is not None:
            return FTPDirectoryProbe(FTPPathProbeResult.EXISTS, frozenset(cached))
        ftp = self._require_ftp()
        try:
            entries = {
                PurePosixPath(entry.rstrip("/")).name
                for entry in ftp.nlst(absolute)
                if entry.rstrip("/")
            }
        except ftplib.error_perm as exc:
            return self._recover_failed_listing(absolute, exc)
        except ftplib.Error as exc:
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=str(exc))
        self._directory_entries[absolute] = entries
        return FTPDirectoryProbe(FTPPathProbeResult.EXISTS, frozenset(entries))

    def _recover_failed_listing(
        self,
        absolute: str,
        error: ftplib.error_perm,
    ) -> FTPDirectoryProbe:
        """Resolve a failed NLST through CWD and the nearest listable ancestor.

        Args:
            absolute: Directory whose listing failed.
            error: Permanent FTP response returned by NLST.

        Returns:
            Missing for an absent parent, Exists for an empty accessible directory,
            or Error when permissions and absence cannot be separated safely.
        """

        detail = str(error)
        if _looks_like_access_denied(detail):
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=detail)
        ftp = self._require_ftp()
        try:
            original = ftp.pwd()
            ftp.cwd(absolute)
            ftp.cwd(original)
        except ftplib.error_perm as cwd_error:
            cwd_detail = str(cwd_error)
            if _looks_like_access_denied(cwd_detail) or absolute == "/":
                return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=cwd_detail)
            parent = PurePosixPath(absolute).parent.as_posix()
            ancestor = self._probe_directory(parent)
            if ancestor.result is not FTPPathProbeResult.EXISTS:
                return ancestor
            name = PurePosixPath(absolute).name
            if name not in ancestor.entries:
                return FTPDirectoryProbe(FTPPathProbeResult.MISSING)
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=cwd_detail)
        except ftplib.Error as cwd_error:
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=str(cwd_error))
        # Several servers report 550 for NLST on an empty directory. Successful
        # CWD proves the parent exists, so the requested child is already absent.
        self._directory_entries[absolute] = set()
        return FTPDirectoryProbe(FTPPathProbeResult.EXISTS)

    def _cache_add(self, parent: str, name: str) -> None:
        """Add a known child only when its parent already has a complete listing."""

        entries = self._directory_entries.get(parent)
        if entries is not None:
            entries.add(name)

    def _cache_discard(self, parent: str, name: str) -> None:
        """Remove a successfully deleted child from a cached complete listing."""

        entries = self._directory_entries.get(parent)
        if entries is not None:
            entries.discard(name)

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


def _looks_like_access_denied(detail: str) -> bool:
    """Recognize explicit access failures while keeping generic 550 replies ambiguous."""

    normalized = detail.casefold()
    return any(
        marker in normalized
        for marker in ("permission", "access denied", "not allowed", "not permitted", "forbidden")
    )


__all__ = ["FTPDirectoryProbe", "FTPPathProbeResult", "FTPTransport"]
