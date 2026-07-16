"""Output hashing and lightweight state durability tests."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from git_deploy.config import OutputConfig
from git_deploy.errors import PlanError, StateError
from git_deploy.manifest import ManifestEntry, StateStore, TargetState, scan_outputs


def test_scans_output_tree_by_remote_path(tmp_path: Path) -> None:
    """Nested build files receive stable SHA256 records at mapped remote paths."""

    output = tmp_path / "dist"
    output.mkdir()
    (output / "app.js").write_text("asset", encoding="utf-8")

    result = scan_outputs((OutputConfig(output, PurePosixPath("public/dist")),))

    assert list(result) == ["public/dist/app.js"]
    assert result["public/dist/app.js"].entry.size == 5
    assert len(result["public/dist/app.js"].entry.sha256) == 64


def test_rejects_output_symlink(tmp_path: Path) -> None:
    """Output scanning cannot follow a symlink to unrelated local data."""

    secret = tmp_path / "secret"
    secret.write_text("no", encoding="utf-8")
    output = tmp_path / "dist"
    output.mkdir()
    (output / "leak").symlink_to(secret)

    with pytest.raises(PlanError, match="symlink"):
        scan_outputs((OutputConfig(output, PurePosixPath("dist")),))


def test_state_is_isolated_and_round_trips(tmp_path: Path) -> None:
    """Each target uses its own schema-validated atomic JSON file."""

    store = StateStore(tmp_path / ".git")
    state = TargetState(1, "dev", "sftp:x", "abc", 123, {"dist/a": ManifestEntry("a" * 64, 1)})

    store.save(state)

    assert store.load("dev") == state
    assert store.load("prod") is None
    assert store.path_for("dev").parent == tmp_path / ".git/git-deploy"


def test_corrupt_state_fails_closed(tmp_path: Path) -> None:
    """Invalid state cannot be mistaken for a first deployment."""

    store = StateStore(tmp_path / ".git")
    path = store.path_for("dev")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 99}), encoding="utf-8")

    with pytest.raises(StateError, match="schema"):
        store.load("dev")
