"""Chunked content providers and owner-only disk spooling."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileContentRef:
    """Validated local content reference for one planned remote path."""

    path: Path
    sha256: str
    size: int


@dataclass
class SpoolHandle:
    """Owned temporary regular file removed explicitly or by provider context."""

    path: Path
    size: int
    sha256: str
    max_chunk_size: int
    _closed: bool = False

    def close(self) -> None:
        """Remove the owned spool file idempotently."""

        if self._closed:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigurationError(f"cannot clean spool file {self.path}: {exc}") from exc
        self._closed = True


class StreamingContentProvider:
    """Read exact regular files per path without aggregating a deployment in memory."""

    def __init__(
        self,
        refs: Mapping[str, FileContentRef],
        spool_dir: Path,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        """Bind planned content refs and an owner-only spool directory."""

        if chunk_size <= 0:
            raise ConfigurationError("stream chunk_size must be positive")
        self.refs = dict(refs)
        self.spool_dir = spool_dir.resolve()
        self.chunk_size = chunk_size

    def iter_chunks(self, path: str) -> Iterator[bytes]:
        """Yield one referenced file in bounded chunks while verifying final identity."""

        ref = self._validated_ref(path)
        digest = hashlib.sha256()
        total = 0
        with ref.path.open("rb") as handle:
            while chunk := handle.read(self.chunk_size):
                if len(chunk) > self.chunk_size:
                    raise ConfigurationError("content provider exceeded chunk budget")
                digest.update(chunk)
                total += len(chunk)
                yield chunk
        if total != ref.size or digest.hexdigest() != ref.sha256:
            raise ConfigurationError(f"content changed while streaming: {path}")

    def consume(self, path: str, consumer: Callable[[bytes], None]) -> int:
        """Send chunks to a consumer and return total bytes consumed."""

        total = 0
        for chunk in self.iter_chunks(path):
            consumer(chunk)
            total += len(chunk)
        return total

    @contextmanager
    def spool(self, path: str) -> Iterator[SpoolHandle]:
        """Copy one content ref to a 0600 spool and clean it on every exception path."""

        self.spool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.spool_dir.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix="git-deploy-spool-", dir=self.spool_dir)
        os.fchmod(descriptor, 0o600)
        spool_path = Path(name)
        digest = hashlib.sha256()
        total = 0
        max_chunk = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in self.iter_chunks(path):
                    handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    max_chunk = max(max_chunk, len(chunk))
                handle.flush()
                os.fsync(handle.fileno())
            handle_ref = SpoolHandle(
                path=spool_path,
                size=total,
                sha256=digest.hexdigest(),
                max_chunk_size=max_chunk,
            )
            try:
                yield handle_ref
            finally:
                handle_ref.close()
        except BaseException:
            try:
                spool_path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _validated_ref(self, path: str) -> FileContentRef:
        """Return a regular, non-symlink file ref with unchanged size metadata."""

        try:
            ref = self.refs[path]
        except KeyError as exc:
            raise ConfigurationError(f"no content reference for planned path: {path}") from exc
        try:
            mode = ref.path.lstat().st_mode
        except OSError as exc:
            raise ConfigurationError(f"cannot stat content reference {ref.path}: {exc}") from exc
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise ConfigurationError(f"content reference is not a regular file: {ref.path}")
        return ref
