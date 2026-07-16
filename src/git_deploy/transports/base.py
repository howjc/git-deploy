"""Narrow remote file contract kept independent of Git, build, and state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

ProgressCallback = Callable[[int, int | None], None]


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

    def __enter__(self) -> Transport:
        """Connect and return this transport as a context manager."""

        self.connect()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        """Close the transport regardless of operation outcome."""

        self.close()
