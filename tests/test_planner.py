"""Source/output merge, safety, and incremental planner tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.config import load_config
from git_deploy.errors import PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import ManifestEntry, TargetState
from git_deploy.planner import DeleteOperation, UploadOperation, create_plan
from tests.conftest import commit_all, write_config


def test_first_plan_uploads_all_managed_source_and_outputs(git_project: Path) -> None:
    """Missing state automatically enters full mode without remote deletion."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "asset.js").write_text("one", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)

    plan = create_plan(config, config.target(None), repository, None, full=False)

    assert plan.full
    assert [(type(item), item.remote_path) for item in plan.operations] == [
        (UploadOperation, "app.py"),
        (UploadOperation, "public/dist/asset.js"),
    ]


def test_incremental_source_and_output_changes(git_project: Path) -> None:
    """Git deletion and changed/new/removed output hashes merge deterministically."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "new.js").write_text("new", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    old = repository.head()
    (git_project / "app.py").unlink()
    commit_all(git_project, "delete source")
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        old,
        1,
        {
            "public/dist/new.js": ManifestEntry("0" * 64, 3),
            "public/dist/old.js": ManifestEntry("1" * 64, 3),
            "remote-unknown.txt": ManifestEntry("2" * 64, 3),
        },
    )

    plan = create_plan(config, config.target(None), repository, state, full=False)

    assert {(type(item), item.remote_path) for item in plan.operations} == {
        (DeleteOperation, "app.py"),
        (UploadOperation, "public/dist/new.js"),
        (DeleteOperation, "public/dist/old.js"),
    }
    assert all(item.remote_path != "remote-unknown.txt" for item in plan.operations)


def test_unchanged_output_is_not_uploaded(git_project: Path) -> None:
    """Equal SHA256 and size records suppress redundant output transfer."""

    dist = git_project / "dist"
    dist.mkdir()
    (dist / "asset.js").write_text("same", encoding="utf-8")
    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    full = create_plan(config, config.target(None), repository, None, full=False)
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        repository.head(),
        1,
        full.output_manifest,
    )

    incremental = create_plan(config, config.target(None), repository, state, full=False)

    assert incremental.operations == ()


def test_modified_source_is_uploaded(git_project: Path) -> None:
    """A committed Git modification produces one exact source upload."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    old = repository.head()
    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    commit_all(git_project, "modify source")
    state = TargetState(1, "dev", config.target(None).fingerprint, old, 1, {})

    plan = create_plan(config, config.target(None), repository, state, full=False)

    assert [(type(item), item.remote_path) for item in plan.operations] == [
        (UploadOperation, "app.py")
    ]


def test_protected_output_destination_fails_closed(git_project: Path) -> None:
    """Final protection also applies to output mappings, not only Git source."""

    (git_project / "dist").mkdir()
    (git_project / "dist/secret").write_text("secret", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
[[outputs]]
local = "dist"
remote = ".env"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
        )
    )

    with pytest.raises(PlanError, match="protected"):
        create_plan(config, config.target(None), GitRepository(git_project), None, full=False)


def test_target_change_requires_explicit_full(git_project: Path) -> None:
    """An existing state cannot silently bind to a different physical target."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    state = TargetState(1, "dev", "sftp:other", repository.head(), 1, {})

    with pytest.raises(PlanError, match="identity changed"):
        create_plan(config, config.target(None), repository, state, full=False)


def test_source_output_remote_collision_is_rejected(git_project: Path) -> None:
    """Two ownership domains cannot publish different bytes to the same remote path."""

    output = git_project / "generated"
    output.mkdir()
    (output / "app.py").write_text("generated", encoding="utf-8")
    config = load_config(
        write_config(
            git_project,
            """
[[outputs]]
local = "generated"
remote = "."

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
        )
    )

    with pytest.raises(PlanError, match="remote path conflict"):
        create_plan(config, config.target(None), GitRepository(git_project), None, full=False)
