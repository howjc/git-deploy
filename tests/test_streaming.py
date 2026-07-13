"""Chunked content provider, spool lifecycle, and resource budget tests."""

from __future__ import annotations

import hashlib
import os
import tracemalloc
from pathlib import Path

import pytest

from git_deploy.streaming import FileContentRef, StreamingContentProvider


def _ref(path: Path) -> FileContentRef:
    """Hash a small fixture into a content reference."""

    data = path.read_bytes()
    return FileContentRef(path, hashlib.sha256(data).hexdigest(), len(data))


def test_provider_reads_each_file_in_bounded_chunks(tmp_path: Path) -> None:
    """Provider streams one file at a time without placing bytes in a plan object."""

    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefghij")
    provider = StreamingContentProvider(
        {"remote.bin": _ref(source)}, tmp_path / "spool", chunk_size=3
    )
    chunks = list(provider.iter_chunks("remote.bin"))
    assert chunks == [b"abc", b"def", b"ghi", b"j"]
    assert max(map(len, chunks)) == 3
    assert provider.refs["remote.bin"].__dict__.keys() == {"path", "sha256", "size"}


def test_spool_permissions_and_success_cleanup(tmp_path: Path) -> None:
    """Spool uses 0600/0700 permissions and disappears after successful use."""

    source = tmp_path / "source.bin"
    source.write_bytes(b"artifact")
    spool_dir = tmp_path / "spool"
    provider = StreamingContentProvider({"a": _ref(source)}, spool_dir, chunk_size=2)
    with provider.spool("a") as handle:
        assert handle.path.read_bytes() == b"artifact"
        assert stat_mode(handle.path) == 0o600
        assert stat_mode(spool_dir) == 0o700
        owned = handle.path
    assert not owned.exists()
    assert list(spool_dir.iterdir()) == []


def test_spool_cleans_on_read_failure_and_consumer_interrupt(tmp_path: Path) -> None:
    """Changed input and interrupted consumers leave no spool files."""

    source = tmp_path / "source.bin"
    source.write_bytes(b"before")
    ref = _ref(source)
    source.write_bytes(b"after-change")
    spool_dir = tmp_path / "spool"
    provider = StreamingContentProvider({"a": ref}, spool_dir, chunk_size=2)
    with pytest.raises(Exception, match="changed"):
        with provider.spool("a"):
            pass
    assert list(spool_dir.iterdir()) == []

    source.write_bytes(b"before")
    provider = StreamingContentProvider({"a": _ref(source)}, spool_dir, chunk_size=2)

    def interrupt(_chunk: bytes) -> None:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        provider.consume("a", interrupt)
    assert list(spool_dir.iterdir()) == []


def test_resource_budget_500mb_streaming(tmp_path: Path) -> None:
    """Five sparse 100 MiB files stay within chunk/RSS budgets and leave no spool."""

    file_size = 100 * 1024 * 1024
    total_size = 5 * file_size
    chunk_size = 1024 * 1024
    refs: dict[str, FileContentRef] = {}
    zero_digest = hashlib.sha256()
    zero_chunk = b"\0" * chunk_size
    for _ in range(file_size // chunk_size):
        zero_digest.update(zero_chunk)
    expected_digest = zero_digest.hexdigest()
    for index in range(5):
        path = tmp_path / f"large-{index}.bin"
        with path.open("wb") as handle:
            handle.truncate(file_size)
        refs[str(index)] = FileContentRef(path, expected_digest, file_size)

    provider = StreamingContentProvider(refs, tmp_path / "spool", chunk_size=chunk_size)
    tracemalloc.start()
    consumed = 0
    max_chunk = 0
    for name in refs:
        for chunk in provider.iter_chunks(name):
            consumed += len(chunk)
            max_chunk = max(max_chunk, len(chunk))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert consumed == total_size
    assert max_chunk <= chunk_size
    assert peak < 16 * 1024 * 1024
    assert not (tmp_path / "spool").exists()


def stat_mode(path: Path) -> int:
    """Return Unix permission bits for a fixture path."""

    return os.stat(path).st_mode & 0o777
