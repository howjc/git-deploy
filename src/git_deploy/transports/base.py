"""Narrow remote file contract kept independent of Git, build, and state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum
from pathlib import Path, PurePosixPath

from git_deploy.errors import DeployError

ProgressCallback = Callable[[int, int | None], None]


def is_stable_remote_component(value: str) -> bool:
    """Return whether one name is stable across supported SFTP adapters.

    Args:
        value: One direct POSIX path component.

    Returns:
        ``True`` for visible names without traversal, separators, or edge spaces.
    """

    return bool(
        value
        and value not in {".", ".."}
        and value == value.strip()
        and "/" not in value
        and "\\" not in value
        and all(
            ord(character) >= 32
            and character != "\x7f"
            and (character == " " or not character.isspace())
            for character in value
        )
    )


class RemotePathType(str, Enum):
    """Classify one remote path without following symbolic links."""

    MISSING = "missing"
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


class Transport(ABC):
    """Define idempotent remote operations used by the deployer."""

    @abstractmethod
    def connect(self) -> None:
        """Open and authenticate one remote session."""

    @abstractmethod
    def ensure_root(self) -> None:
        """Ensure the configured remote root exists and is writable."""

    @abstractmethod
    def root_exists(self) -> bool:
        """Return whether the configured remote root currently exists."""

    @abstractmethod
    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Upload one local file to a safe relative remote path.

        Args:
            local_path: Frozen local file to stream.
            remote_path: Normalized relative path below the configured root.
            callback: Progress callback receiving transferred and total bytes.
            executable: Whether SFTP should publish the file with mode ``0755``.
        """

    @abstractmethod
    def delete(self, remote_path: str) -> None:
        """Idempotently delete one relative remote file.

        Args:
            remote_path: Normalized relative path below the configured root.
        """

    @abstractmethod
    def close(self) -> None:
        """Close all network resources, tolerating repeated calls."""

    def invalidate_connection(self) -> None:
        """Discard a failed connection before retrying it.

        Returns:
            ``None`` after the default transport resources are closed.
        """

        self.close()

    def run_command(
        self,
        command: str,
        *,
        cwd: PurePosixPath,
        timeout: float | None,
    ) -> None:
        """Run one non-interactive remote command when the backend supports it.

        Args:
            command: Trusted, validated one-line shell command.
            cwd: Absolute remote working directory.
            timeout: Optional whole-command timeout in seconds.

        Returns:
            ``None`` after a zero exit status.
        """

        raise DeployError("remote commands are not supported by this transport")

    def lstat(self, remote_path: str) -> RemotePathType:
        """Classify one safe relative path without following symlinks.

        Args:
            remote_path: Relative path below the configured remote root.

        Returns:
            Explicit path type, including confirmed absence.
        """

        raise DeployError("remote path inspection is not supported by this transport")

    def read_file(self, remote_path: str, *, max_bytes: int) -> bytes:
        """Read one bounded regular file without following symlinks.

        Args:
            remote_path: Relative path below the configured root.
            max_bytes: Maximum accepted file size.

        Returns:
            Exact remote bytes.
        """

        raise DeployError("remote file reads are not supported by this transport")

    def write_file_atomic(self, remote_path: str, data: bytes) -> None:
        """Atomically publish small internal metadata bytes.

        Args:
            remote_path: Protected relative metadata path.
            data: Complete bytes to publish as mode ``0644``.

        Returns:
            ``None`` after safe temporary replacement.
        """

        raise DeployError("remote metadata writes are not supported by this transport")

    def list_directory(self, remote_path: str) -> tuple[str, ...]:
        """Return stable direct child names without recursive traversal."""

        raise DeployError("remote directory listing is not supported by this transport")

    def make_directory(self, remote_path: str, *, mode: int = 0o755) -> None:
        """Create one relative directory and missing parents idempotently.

        Args:
            remote_path: Safe relative directory path.
            mode: POSIX permission requested for created/final directory.
        """

        raise DeployError("remote directory creation is not supported by this transport")

    def rename_path(self, source: str, destination: str) -> None:
        """Atomically rename one path only if destination is absent remotely."""

        raise DeployError("remote rename is not supported by this transport")

    def remove_tree(self, remote_path: str) -> None:
        """Remove one file/directory tree without following symlinks."""

        raise DeployError("remote recursive removal is not supported by this transport")

    def __enter__(self) -> Transport:
        """Connect and return this transport as a context manager."""

        self.connect()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        """Close the transport regardless of operation outcome."""

        self.close()
