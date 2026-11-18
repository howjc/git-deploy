"""Regression tests for batched FTP IN-PLACE Hybrid publication."""

from __future__ import annotations

import ftplib
import hashlib
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

import pytest

import git_deploy.deployer as deployer_module
from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError
from git_deploy.planner import FTPHybridFileUpload
from git_deploy.progress import ProgressReporter
from git_deploy.transports.ftp import FTPTransport


_GROUP = "assets/js"
_STAGE_ROOT = ".git-deploy/ftp-hybrid/stage/deployment-batch"
_FILE_NAMES = ("app.js", "chunk.js", "runtime.js", "vendor.js")


def _target() -> TargetConfig:
    """Return one deterministic FTP target for adapter-level tests."""

    return TargetConfig(
        "prod",
        "ftp",
        "ftp.example.invalid",
        "deploy",
        PurePosixPath("/root"),
        21,
        password_env="FTP_PASSWORD",
    )


class _FakeFTPSession:
    """Model the raw ftplib calls used by the real FTPTransport adapter."""

    def __init__(self) -> None:
        """Create an empty remote root and call counters."""

        self.directories = {"/", "/root"}
        self.files: dict[str, bytes] = {}
        self.foreign_types: dict[str, str] = {}
        self.mlsd_calls: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.rename_failures: dict[str, int] = {}
        self.lost_rename_replies: dict[str, int] = {}
        self.retr_commands: list[str] = []
        self.stor_paths: list[str] = []
        self.quit_calls = 0
        self.leave_source = False
        self.final_type: str | None = None
        self.corrupt_final = False
        self.current_directory = "/"

    def mlsd(self, path: str) -> Iterator[tuple[str, dict[str, str]]]:
        """Render direct children from the mutable remote tree."""

        normalized = PurePosixPath(path).as_posix()
        self.mlsd_calls.append(normalized)
        if normalized not in self.directories:
            raise ftplib.error_perm("550 No such directory")

        entries: dict[str, dict[str, str]] = {}
        for directory in self.directories:
            child = PurePosixPath(directory)
            if child.parent.as_posix() == normalized and directory != normalized:
                entries[child.name] = {"type": "dir"}
        for file_path, payload in self.files.items():
            child = PurePosixPath(file_path)
            if child.parent.as_posix() == normalized:
                entries[child.name] = {"type": "file", "size": str(len(payload))}
        for child_path, kind in self.foreign_types.items():
            child = PurePosixPath(child_path)
            if child.parent.as_posix() == normalized:
                entries[child.name] = {"type": kind}
        return iter(tuple(sorted(entries.items())))

    def rename(self, source: str, destination: str) -> None:
        """Model rename-replace and injectable server/postcondition defects."""

        self.rename_calls.append((source, destination))
        remaining_failures = self.rename_failures.get(destination, 0)
        if remaining_failures:
            self.rename_failures[destination] = remaining_failures - 1
            raise ftplib.error_temp("421 Control connection interrupted")
        if source not in self.files:
            raise ftplib.error_perm(f"550 Missing source {source}")

        payload = self.files[source]
        if not self.leave_source:
            del self.files[source]
        if self.final_type == "dir":
            self.directories.add(destination)
            self.foreign_types.pop(destination, None)
        elif self.final_type is not None:
            self.foreign_types[destination] = self.final_type
        else:
            self.files[destination] = (
                payload + b"corrupt" if self.corrupt_final else payload
            )
        remaining_lost_replies = self.lost_rename_replies.get(destination, 0)
        if remaining_lost_replies:
            self.lost_rename_replies[destination] = remaining_lost_replies - 1
            raise ftplib.error_temp("426 Reply lost after rename")

    def retrbinary(
        self,
        command: str,
        callback: Callable[[bytes], None],
    ) -> None:
        """Stream one final/stage file and record the exact RETR command."""

        self.retr_commands.append(command)
        path = command.removeprefix("RETR ")
        try:
            payload = self.files[path]
        except KeyError as exc:
            raise ftplib.error_perm(f"550 Missing RETR target {path}") from exc
        callback(payload)

    def storbinary(
        self,
        command: str,
        handle: BinaryIO,
        *,
        blocksize: int,
        callback: Callable[[bytes], None],
    ) -> None:
        """Store one restaged file so an interrupted batch can resume."""

        del blocksize
        path = command.removeprefix("STOR ")
        payload = handle.read()
        self.files[path] = payload
        self.stor_paths.append(path)
        callback(payload)

    def mkd(self, path: str) -> None:
        """Create a directory or report the existing FTP 550 convention."""

        if path in self.directories or path in self.files:
            raise ftplib.error_perm("550 Directory already exists")
        self.directories.add(path)

    def cwd(self, path: str) -> None:
        """Enter an existing remote directory."""

        if path not in self.directories:
            raise ftplib.error_perm("550 No such directory")
        self.current_directory = path

    def pwd(self) -> str:
        """Return the synthetic current directory."""

        return self.current_directory

    def quit(self) -> None:
        """Accept invalidation of a dropped control session."""

        self.quit_calls += 1


def _ensure_directory_tree(session: _FakeFTPSession, absolute: str) -> None:
    """Add every ancestor required for one absolute synthetic directory."""

    current = PurePosixPath("/")
    for component in PurePosixPath(absolute).parts[1:]:
        current = current / component
        session.directories.add(current.as_posix())


def _batch_fixture(
    tmp_path: Path,
    *,
    file_names: tuple[str, ...] = _FILE_NAMES,
    group: str = _GROUP,
) -> tuple[
    FTPTransport,
    _FakeFTPSession,
    tuple[FTPHybridFileUpload, ...],
    dict[str, Path],
]:
    """Create frozen same-parent uploads plus their remote Stage files."""

    frozen_root = tmp_path / "frozen"
    frozen_root.mkdir()
    session = _FakeFTPSession()
    # Root may contain types that FTPTransport deliberately does not classify.
    session.foreign_types["/root/vendor-owned"] = "OS.unix=symlink"
    staged_parent = f"/root/{_STAGE_ROOT}/files"
    if group:
        staged_parent = f"{staged_parent}/{group}"
    final_parent = f"/root/{group}" if group else "/root"
    _ensure_directory_tree(session, staged_parent)
    _ensure_directory_tree(session, final_parent)

    uploads: list[FTPHybridFileUpload] = []
    frozen: dict[str, Path] = {}
    for name in file_names:
        logical_path = f"{group}/{name}" if group else name
        local_path = frozen_root / name
        payload = f"payload:{logical_path}".encode("utf-8")
        local_path.write_bytes(payload)
        frozen[logical_path] = local_path
        uploads.append(
            FTPHybridFileUpload(
                logical_path,
                local_path,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
            )
        )
        session.files[f"/root/{_STAGE_ROOT}/files/{logical_path}"] = payload

    transport = FTPTransport(_target())
    transport.ftp = cast(Any, session)
    return transport, session, tuple(uploads), frozen


def _publish_batch(
    transport: FTPTransport,
    uploads: tuple[FTPHybridFileUpload, ...],
    frozen: dict[str, Path],
    *,
    attempts: int = 1,
    verify_final: bool = False,
) -> None:
    """Call the batch publisher through the production FTPTransport adapter."""

    deployer_module._publish_ftp_hybrid_batch(
        uploads,
        frozen,
        _STAGE_ROOT,
        transport,
        ProgressReporter(verbose=False),
        attempts=attempts,
        delay=0,
        verify_final=verify_final,
    )


def _stage_path(upload: FTPHybridFileUpload) -> str:
    """Return one absolute synthetic Stage path."""

    return f"/root/{_STAGE_ROOT}/files/{upload.path}"


def _final_path(upload: FTPHybridFileUpload) -> str:
    """Return one absolute synthetic final path."""

    return f"/root/{upload.path}"


def _install_session_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    transport: FTPTransport,
    session: _FakeFTPSession,
) -> None:
    """Restore the fake raw session after the production invalidation hook."""

    def restore_session(worker: FTPTransport, **kwargs: object) -> None:
        """Reattach the surviving fake server after a dropped control session."""

        del kwargs
        worker.ftp = cast(Any, session)

    monkeypatch.setattr(deployer_module, "_connect_with_retry", restore_session)


def test_ftp_batch_lists_nested_parents_once_amid_unknown_root_types(
    tmp_path: Path,
) -> None:
    """Nested siblings use one target and two Stage parent snapshots."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)

    _publish_batch(transport, uploads, frozen)

    stage_parent = f"/root/{_STAGE_ROOT}/files/{_GROUP}"
    final_parent = f"/root/{_GROUP}"
    mlsd_counts = Counter(session.mlsd_calls)
    assert mlsd_counts[stage_parent] == 2
    assert mlsd_counts[final_parent] == 1
    ancestor_counts = {
        path: count
        for path, count in mlsd_counts.items()
        if path not in {stage_parent, final_parent}
    }
    assert ancestor_counts
    assert max(ancestor_counts.values()) <= 3
    assert sorted(session.rename_calls) == sorted(
        (_stage_path(upload), _final_path(upload)) for upload in uploads
    )
    assert session.retr_commands == []
    for upload in uploads:
        assert session.files[_final_path(upload)] == frozen[upload.path].read_bytes()
        assert _stage_path(upload) not in session.files


def test_ftp_batch_root_parent_ignores_unrelated_unknown_types(
    tmp_path: Path,
) -> None:
    """Root-level files publish while an unrelated root entry has an unknown type."""

    transport, session, uploads, frozen = _batch_fixture(
        tmp_path,
        file_names=("index.html", "robots.txt"),
        group="",
    )

    _publish_batch(transport, uploads, frozen)

    assert session.foreign_types["/root/vendor-owned"] == "OS.unix=symlink"
    assert sorted(session.rename_calls) == sorted(
        (_stage_path(upload), _final_path(upload)) for upload in uploads
    )
    for upload in uploads:
        assert session.files[_final_path(upload)] == frozen[upload.path].read_bytes()
        assert _stage_path(upload) not in session.files


def test_ftp_batch_rejects_rename_that_does_not_consume_stage(
    tmp_path: Path,
) -> None:
    """A successful RNTO that leaves Stage bytes cannot be reported as success."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)
    session.leave_source = True

    with pytest.raises(DeployError):
        _publish_batch(transport, uploads, frozen)

    assert session.rename_calls
    assert any(_stage_path(upload) in session.files for upload in uploads)


@pytest.mark.parametrize("final_type", ["dir", "OS.unix=symlink"])
def test_ftp_batch_rejects_non_file_final_entries(
    tmp_path: Path,
    final_type: str,
) -> None:
    """Rename ordering alone is insufficient when the final entry is not File."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)
    session.final_type = final_type

    with pytest.raises(DeployError):
        _publish_batch(transport, uploads, frozen)

    assert session.rename_calls


def test_ftp_batch_verify_final_retrieves_each_published_file(
    tmp_path: Path,
) -> None:
    """Strong verification RETR-SHA256s all final files after the batch rename."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)

    _publish_batch(transport, uploads, frozen, verify_final=True)

    expected = sorted(f"RETR {_final_path(upload)}" for upload in uploads)
    assert sorted(session.retr_commands) == expected
    assert all(
        f"/{_STAGE_ROOT}/" not in command for command in session.retr_commands
    )


def test_ftp_batch_verify_final_rejects_corrupt_final_bytes(
    tmp_path: Path,
) -> None:
    """A regular-looking but changed final payload fails strong verification."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)
    session.corrupt_final = True

    with pytest.raises(DeployError):
        _publish_batch(transport, uploads, frozen, verify_final=True)

    assert session.retr_commands


def test_ftp_batch_retry_accepts_partial_rename_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped second RNTO retries without republishing or losing the first."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)
    interrupted = uploads[1]
    session.rename_failures[_final_path(interrupted)] = 1

    _install_session_reconnect(monkeypatch, transport, session)

    _publish_batch(transport, uploads, frozen, attempts=2)

    interrupted_rename = (_stage_path(interrupted), _final_path(interrupted))
    assert session.rename_calls.count(interrupted_rename) == 2
    for upload in uploads:
        assert session.files[_final_path(upload)] == frozen[upload.path].read_bytes()
        assert _stage_path(upload) not in session.files


def test_ftp_batch_reconciles_lost_successful_rename_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An RNTOReply loss after server commit restages once and converges."""

    transport, session, uploads, frozen = _batch_fixture(tmp_path)
    uncertain = uploads[1]
    destination = _final_path(uncertain)
    session.lost_rename_replies[destination] = 1
    _install_session_reconnect(monkeypatch, transport, session)

    _publish_batch(transport, uploads, frozen, attempts=2)

    rename = (_stage_path(uncertain), destination)
    assert session.rename_calls.count(rename) == 2
    assert session.stor_paths == [_stage_path(uncertain)]
    assert session.files[destination] == frozen[uncertain.path].read_bytes()
    assert _stage_path(uncertain) not in session.files
    for upload in uploads:
        assert session.files[_final_path(upload)] == frozen[upload.path].read_bytes()
        assert _stage_path(upload) not in session.files


def test_ftp_batch_retries_corrupt_restage_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed restage checksum never turns leftover bytes into a publishable file."""
    transport, session, uploads, frozen = _batch_fixture(tmp_path)
    missing = uploads[0]
    stage = _stage_path(missing)
    del session.files[stage]
    original_stor = session.storbinary

    def store_once_corrupt(
        command: str, handle: BinaryIO, *, blocksize: int, callback: Callable[[bytes], None]
    ) -> None:
        original_stor(command, handle, blocksize=blocksize, callback=callback)
        if session.stor_paths == [stage]:
            session.files[stage] = b"?" * missing.size

    monkeypatch.setattr(session, "storbinary", store_once_corrupt)
    _install_session_reconnect(monkeypatch, transport, session)
    _publish_batch(transport, uploads, frozen, attempts=2)

    assert session.stor_paths == [stage, stage]
    assert session.rename_calls.count((stage, _final_path(missing))) == 1
    assert session.files[_final_path(missing)] == frozen[missing.path].read_bytes()
