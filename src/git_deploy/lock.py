"""Small per-target process lock shared by every worktree of one repository."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path
from types import TracebackType

from git_deploy.errors import PlanError


class TargetLock:
    """Hold a non-blocking advisory lock for one repository target."""

    def __init__(self, state_root: Path, target: str) -> None:
        """Configure a lock below the common Git state directory.

        Args:
            state_root: ``<git-common-dir>/git-deploy`` directory.
            target: Validated target name used for the lock filename.
        """

        self.path = state_root / f"{target}.lock"
        self.target = target
        self._handle = None

    def acquire(self) -> None:
        """Acquire the lock or report the current owner without waiting."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "owner information unavailable"
            handle.close()
            raise PlanError(
                f"target {self.target} is already being deployed by another process: {owner}"
            ) from exc
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": int(time.time()),
            "target": self.target,
        }
        handle.seek(0)
        handle.truncate()
        json.dump(owner, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        """Release the lock idempotently while retaining the safe inode path."""

        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> TargetLock:
        """Acquire and return the lock for a deployment scope."""

        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock on success, failure, or interruption."""

        self.release()
