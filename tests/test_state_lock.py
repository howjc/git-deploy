"""Target lock exclusivity tests (S09)."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError
from git_deploy.target_lock import TargetLock, try_lock


def _hold_lock(root: str, ready: multiprocessing.Queue, release: multiprocessing.Queue) -> None:
    """Child process helper: acquire lock, signal ready, wait for release.

    Args:
        root: Target root path string.
        ready: Queue signaling acquisition.
        release: Queue waiting for parent release signal.

    Returns:
        None.
    """

    lock = TargetLock(Path(root))
    lock.acquire()
    ready.put("ready")
    release.get()
    lock.release()


def test_same_target_exclusive_different_targets_independent(tmp_path: Path) -> None:
    """Two processes contend for one target; different targets do not block."""

    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    first = TargetLock(target_a)
    first.acquire()
    with pytest.raises(ConfigurationError, match="lock unavailable"):
        TargetLock(target_a, timeout=0.0).acquire()
    second = TargetLock(target_b)
    second.acquire()
    second.release()
    first.release()


def test_lock_recoverable_after_process_exit(tmp_path: Path) -> None:
    """When a process exits, the next process can acquire the same target lock."""

    root = tmp_path / "t"
    ready: multiprocessing.Queue[str] = multiprocessing.Queue()
    release: multiprocessing.Queue[str] = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_hold_lock, args=(str(root), ready, release))
    proc.start()
    assert ready.get(timeout=5) == "ready"
    assert try_lock(root) is None
    release.put("done")
    proc.join(timeout=5)
    assert proc.exitcode == 0
    recovered = try_lock(root)
    assert recovered is not None
    recovered.release()
