"""Durable atomic publisher contract tests (S05A)."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.durable_io import (
    check_durable_filesystem,
    cleanup_orphan_temps,
    durable_publish,
    file_mode,
    list_orphan_temps,
    set_fault_hook,
    set_filesystem_capable,
)
from git_deploy.errors import ConfigurationError


@pytest.fixture(autouse=True)
def _reset_hooks() -> None:
    """Clear fault and capability overrides after each test.

    Returns:
        None.
    """

    yield
    set_fault_hook(None)
    set_filesystem_capable(None)


def test_durable_publish_permissions_and_content(tmp_path: Path) -> None:
    """Publish with 0600 file and 0700 parent directory permissions."""

    target = tmp_path / "state" / "current.json"
    durable_publish(target, b'{"ok":true}\n')
    assert target.read_bytes() == b'{"ok":true}\n'
    assert file_mode(target) == 0o600
    assert file_mode(target.parent) == 0o700


@pytest.mark.parametrize(
    "point",
    ["before_write", "after_write", "after_file_fsync", "after_replace"],
)
def test_fault_before_completion_leaves_old_or_absent(tmp_path: Path, point: str) -> None:
    """Kill/fault mid-publish leaves only complete old content or no file."""

    target = tmp_path / "value.json"
    durable_publish(target, b"old\n")

    def fault(stage: str, path: Path) -> None:
        if stage == point:
            raise RuntimeError(f"injected fault at {stage}")

    set_fault_hook(fault)
    with pytest.raises(RuntimeError, match="injected fault"):
        durable_publish(target, b"new\n")

    if point in {"before_write", "after_write", "after_file_fsync"}:
        assert target.read_bytes() == b"old\n"
    # after_replace may leave new content because rename already committed;
    # never a partial mix of old+new bytes.
    if target.exists():
        assert target.read_bytes() in {b"old\n", b"new\n"}
    orphans = list_orphan_temps(tmp_path)
    for orphan in orphans:
        assert orphan.name.endswith(".tmp")
        assert orphan != target


def test_orphan_temps_not_visible_as_current_and_cleanup(tmp_path: Path) -> None:
    """Orphan temps are reportable, cleanable, and never the published path."""

    target = tmp_path / "current.json"
    durable_publish(target, b"complete\n")
    orphan = tmp_path / ".current.json.12345.tmp"
    orphan.write_bytes(b"partial")
    found = list_orphan_temps(tmp_path)
    assert orphan in found
    assert target not in found
    removed = cleanup_orphan_temps(tmp_path)
    assert orphan in removed
    assert not orphan.exists()
    assert target.read_bytes() == b"complete\n"


def test_unsupported_filesystem_rejected_before_publish(tmp_path: Path) -> None:
    """State filesystems lacking durable capability are refused up front."""

    set_filesystem_capable(False)
    target = tmp_path / "current.json"
    with pytest.raises(ConfigurationError, match="durable atomic publish"):
        durable_publish(target, b"x")
    assert not target.exists()


def test_check_durable_filesystem_probe_passes_on_local_posix(tmp_path: Path) -> None:
    """Local POSIX temp dirs satisfy the durability probe."""

    check_durable_filesystem(tmp_path / "state")
