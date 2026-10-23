"""OS-backed file locks released automatically when a process exits."""

from __future__ import annotations

import errno
import os
import time
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Windows byte-range locks also prohibit reading the locked bytes. Lock beyond
# EOF so contenders can still read owner metadata, and truncation stays safe.
_LOCK_OFFSET = 0x7FFFFFFF


def lock_file(handle: IO[str], *, blocking: bool = False) -> None:
    """Exclusively lock an open file, raising BlockingIOError if it is busy."""

    if os.name != "nt":
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    position = handle.tell()
    try:
        handle.seek(_LOCK_OFFSET)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not blocking:
                    raise BlockingIOError(exc.errno, "file is already locked") from exc
                time.sleep(0.05)
    finally:
        handle.seek(position)


def unlock_file(handle: IO[str]) -> None:
    """Release a lock without changing the caller's file position."""

    if os.name != "nt":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    position = handle.tell()
    try:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.seek(position)
