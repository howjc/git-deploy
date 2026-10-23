"""Local filesystem operations whose contracts differ on Windows."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def sync_directory(path: Path) -> None:
    """Sync a renamed directory entry on POSIX after its file was fsynced.

    Windows does not support opening directories with os.open/os.fsync. Files
    are still flushed and closed before os.replace on both platforms.
    """

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def is_link(path: Path) -> bool:
    """Identify symlinks and Windows reparse points, including NTFS junctions."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
