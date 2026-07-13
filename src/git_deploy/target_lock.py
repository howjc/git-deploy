"""Local exclusive lock for one physical target_id.

v0.2 only promises single-controller local exclusivity, not distributed locking.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

from .durable_io import ensure_state_directory
from .errors import ConfigurationError


class TargetLock:
    """``fcntl`` flock-based exclusive lock stored under the target state root."""

    def __init__(self, target_root: Path, *, timeout: float = 0.0):
        """Create a lock handle without acquiring it.

        Args:
            target_root: ``.../targets/<target-id>`` directory.
            timeout: Seconds to wait for the lock; ``0`` means non-blocking once.
        """

        self.target_root = target_root.resolve()
        self.lock_path = self.target_root / "target.lock"
        self.timeout = timeout
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the exclusive lock or raise when unavailable.

        Returns:
            None.

        Raises:
            ConfigurationError: When the lock cannot be acquired.
        """

        import fcntl

        ensure_state_directory(self.target_root)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout if self.timeout > 0 else None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                # Record owner pid for diagnostics; lock is still kernel-owned.
                os.ftruncate(fd, 0)
                os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                return
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    os.close(fd)
                    raise ConfigurationError(
                        f"target lock unavailable: {self.lock_path}"
                    ) from None
                time.sleep(0.05)

    def release(self) -> None:
        """Release a held lock.

        Returns:
            None.
        """

        import fcntl

        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> TargetLock:
        """Context-manager acquire.

        Returns:
            Self.
        """

        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Context-manager release.

        Args:
            exc_type: Exception type if any.
            exc: Exception instance if any.
            tb: Traceback if any.

        Returns:
            None.
        """

        self.release()

    @property
    def held(self) -> bool:
        """Return whether this instance currently holds the lock.

        Returns:
            ``True`` when an open locked fd is retained.
        """

        return self._fd is not None


def try_lock(target_root: Path) -> TargetLock | None:
    """Attempt a non-blocking lock acquire.

    Args:
        target_root: Target state root.

    Returns:
        Held lock, or ``None`` when busy.
    """

    lock = TargetLock(target_root, timeout=0.0)
    try:
        lock.acquire()
    except ConfigurationError:
        return None
    return lock
