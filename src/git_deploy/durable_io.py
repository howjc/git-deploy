"""Permission-controlled durable atomic publisher for state artifacts.

Critical local state (current pointer, immutable snapshots, CAS objects,
transaction journals, deployment backups, migration records) must survive
process kill and host reboot as either complete old or complete new content.
Plain ``os.replace`` is not enough: file and parent-directory fsync are required.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from .errors import ConfigurationError

FaultPoint = Literal[
    "before_write",
    "after_write",
    "after_file_fsync",
    "after_replace",
    "after_dir_fsync",
]

# Injected only by tests to simulate crash/kill at each durable-publish stage.
_FAULT_HOOK: Callable[[FaultPoint, Path], None] | None = None

# Filesystem capability probe can be forced-fail for unsupported backends.
_FS_CAPABLE: bool | None = None

_TEMP_SUFFIX = ".tmp"
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def set_fault_hook(hook: Callable[[FaultPoint, Path], None] | None) -> None:
    """Install or clear a test-only fault injection hook.

    Args:
        hook: Callback invoked at each publish stage, or ``None`` to clear.

    Returns:
        None.
    """

    global _FAULT_HOOK
    _FAULT_HOOK = hook


def set_filesystem_capable(capable: bool | None) -> None:
    """Override filesystem durability capability for tests.

    Args:
        capable: ``True``/``False`` force result, or ``None`` to probe normally.

    Returns:
        None.
    """

    global _FS_CAPABLE
    _FS_CAPABLE = capable


def ensure_state_directory(path: Path) -> Path:
    """Create a state directory with owner-only permissions when missing.

    Args:
        path: Absolute or relative directory that must exist for state writes.

    Returns:
        Resolved directory path.
    """

    path = path.resolve()
    path.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
    try:
        path.chmod(_DIR_MODE)
    except OSError:
        pass
    return path


def check_durable_filesystem(path: Path) -> None:
    """Reject state filesystems that cannot provide durable atomic publish.

    Args:
        path: Directory that will hold durable state files.

    Returns:
        None.

    Raises:
        ConfigurationError: When atomic rename or fsync cannot be relied upon.
    """

    path = ensure_state_directory(path)
    if _FS_CAPABLE is False:
        raise ConfigurationError(
            f"state filesystem at {path} does not support durable atomic publish "
            "(atomic rename + file/directory fsync required)"
        )
    if _FS_CAPABLE is True:
        return
    # Best-effort probe: same-directory rename and fsync must succeed.
    probe = path / f".durable-probe.{os.getpid()}"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        renamed = path / f".durable-probe-renamed.{os.getpid()}"
        os.replace(probe, renamed)
        dir_fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        renamed.unlink(missing_ok=True)
    except OSError as exc:
        raise ConfigurationError(
            f"state filesystem at {path} does not support durable atomic publish: {exc}"
        ) from exc
    finally:
        probe.unlink(missing_ok=True)


def durable_publish(path: Path, data: bytes) -> None:
    """Publish bytes via write→flush/fsync→atomic replace→parent directory fsync.

    Args:
        path: Final durable path (never an orphan ``*.tmp``).
        data: Complete payload to persist.

    Returns:
        None.

    Raises:
        ConfigurationError: When the state filesystem is not durable-capable.
        OSError: Propagated from underlying IO failures after cleanup.
    """

    path = path.resolve()
    parent = ensure_state_directory(path.parent)
    check_durable_filesystem(parent)
    _fire("before_write", path)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=_TEMP_SUFFIX,
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, _FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            _fire("after_write", path)
            os.fsync(handle.fileno())
            _fire("after_file_fsync", path)
        os.replace(temporary, path)
        _fire("after_replace", path)
        dir_fd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        _fire("after_dir_fsync", path)
        try:
            path.chmod(_FILE_MODE)
        except OSError:
            pass
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def list_orphan_temps(directory: Path) -> list[Path]:
    """Report temporary publish files that must never become current/journal.

    Args:
        directory: Directory to scan (non-recursive).

    Returns:
        Sorted orphan temporary paths ending in ``.tmp``.
    """

    directory = Path(directory)
    if not directory.is_dir():
        return []
    orphans: list[Path] = []
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.endswith(_TEMP_SUFFIX):
            orphans.append(entry)
    return sorted(orphans)


def cleanup_orphan_temps(directory: Path) -> list[Path]:
    """Delete reported orphan temporary files and return what was removed.

    Args:
        directory: Directory previously scanned for orphan temps.

    Returns:
        Paths successfully unlinked.
    """

    removed: list[Path] = []
    for orphan in list_orphan_temps(directory):
        try:
            orphan.unlink()
            removed.append(orphan)
        except OSError:
            continue
    return removed


def is_visible_state_file(path: Path) -> bool:
    """Return whether a path is a durable published artifact (not a temp).

    Args:
        path: Candidate state file path.

    Returns:
        ``True`` when the name is not a publish temporary.
    """

    return not path.name.endswith(_TEMP_SUFFIX)


def file_mode(path: Path) -> int:
    """Return the permission bits for one path.

    Args:
        path: Existing file or directory.

    Returns:
        Mode bits masked to ``0o777``.
    """

    return stat.S_IMODE(path.stat().st_mode)


def _fire(point: FaultPoint, path: Path) -> None:
    """Invoke the optional test fault hook.

    Args:
        point: Durable publish stage name.
        path: Final destination path being published.

    Returns:
        None.
    """

    if _FAULT_HOOK is not None:
        _FAULT_HOOK(point, path)
