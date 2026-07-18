"""Executable reference aggregation script contract tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "examples/aggregate_frontend_builds.py"


def test_reference_aggregator_merges_explicit_sources_and_atomically_replaces(
    tmp_path: Path,
) -> None:
    """The example produces one final view and removes stale prior content."""

    (tmp_path / "frontend/dist/assets").mkdir(parents=True)
    (tmp_path / "admin/dist/images").mkdir(parents=True)
    (tmp_path / "frontend/dist/index.html").write_text("index", encoding="utf-8")
    (tmp_path / "frontend/dist/assets/app.js").write_text("app", encoding="utf-8")
    (tmp_path / "admin/dist/images/logo.svg").write_text("logo", encoding="utf-8")
    old = tmp_path / ".deploy/frontend-root"
    old.mkdir(parents=True)
    (old / "stale.js").write_text("stale", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (old / "index.html").read_text(encoding="utf-8") == "index"
    assert (old / "assets/app.js").read_text(encoding="utf-8") == "app"
    assert (old / "images/logo.svg").read_text(encoding="utf-8") == "logo"
    assert not (old / "stale.js").exists()


def test_reference_aggregator_rejects_duplicate_and_symlink_without_losing_old_view(
    tmp_path: Path,
) -> None:
    """Duplicate paths and symlinks fail closed before the destination swap."""

    (tmp_path / "frontend/dist/assets").mkdir(parents=True)
    (tmp_path / "admin/dist/assets").mkdir(parents=True)
    (tmp_path / "frontend/dist/assets/app.js").write_text("first", encoding="utf-8")
    (tmp_path / "admin/dist/assets/app.js").write_text("second", encoding="utf-8")
    old = tmp_path / ".deploy/frontend-root"
    old.mkdir(parents=True)
    (old / "known-good").write_text("old", encoding="utf-8")

    duplicate = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert duplicate.returncode != 0
    assert "duplicate or file/directory conflict" in duplicate.stderr
    assert (old / "known-good").read_text(encoding="utf-8") == "old"

    (tmp_path / "admin/dist/assets/app.js").unlink()
    (tmp_path / "admin/dist/escape").symlink_to(tmp_path / "frontend/dist/index.html")
    symlink = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert symlink.returncode != 0
    assert "symbolic links are not supported" in symlink.stderr
    assert (old / "known-good").read_text(encoding="utf-8") == "old"


def test_reference_aggregator_rejects_symlinked_destination_component(
    tmp_path: Path,
) -> None:
    """A destination symlink cannot redirect the atomic swap to another directory."""

    (tmp_path / "frontend/dist").mkdir(parents=True)
    (tmp_path / "admin/dist").mkdir(parents=True)
    (tmp_path / "frontend/dist/index.html").write_text("index", encoding="utf-8")
    actual = tmp_path / "actual-output"
    actual.mkdir()
    (actual / "known-good").write_text("old", encoding="utf-8")
    (tmp_path / ".deploy").mkdir()
    (tmp_path / ".deploy/frontend-root").symlink_to(actual, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "contains a symbolic link" in result.stderr
    assert (actual / "known-good").read_text(encoding="utf-8") == "old"


def test_reference_aggregator_rejects_core_protected_and_unstable_names(
    tmp_path: Path,
) -> None:
    """The example enforces the same direct-name boundary as Hybrid Core."""

    (tmp_path / "frontend/dist/.git").mkdir(parents=True)
    (tmp_path / "admin/dist").mkdir(parents=True)
    (tmp_path / "frontend/dist/.git/config").write_text("secret", encoding="utf-8")
    destination = tmp_path / ".deploy/frontend-root"
    destination.mkdir(parents=True)
    (destination / "known-good").write_text("old", encoding="utf-8")

    protected = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert protected.returncode != 0
    assert "unsafe aggregate path" in protected.stderr
    assert (destination / "known-good").read_text(encoding="utf-8") == "old"

    (tmp_path / "frontend/dist/.git/config").unlink()
    (tmp_path / "frontend/dist/.git").rmdir()
    (tmp_path / "frontend/dist/trailing ").mkdir()
    unstable = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert unstable.returncode != 0
    assert "unsafe aggregate path" in unstable.stderr
    assert (destination / "known-good").read_text(encoding="utf-8") == "old"
