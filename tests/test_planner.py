"""Source/output merge, safety, and incremental planner tests."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from git_deploy.config import load_config
from git_deploy.errors import PlanError
from git_deploy.git import GitRepository
from git_deploy.manifest import ManifestEntry, TargetState
from git_deploy.planner import DeleteOperation, UploadOperation, create_plan, render_plan
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


def test_plan_freezes_and_renders_reviewed_after_deploy_commands(git_project: Path) -> None:
    """The confirmation surface includes commands frozen into the resolved target."""

    config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
after_deploy = ["restart-app", "check-app"]
""",
        )
    )

    plan = create_plan(config, config.target(None), GitRepository(git_project), None, full=False)
    rendered = render_plan(plan)

    assert plan.target.after_deploy == ("restart-app", "check-app")
    assert "AFTER  restart-app" in rendered
    assert "AFTER  check-app" in rendered
    assert "2 after-deploy command(s)" in rendered


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


def test_existing_empty_output_root_can_delete_owned_manifest_files(git_project: Path) -> None:
    """An existing empty directory is an explicit current set, unlike a missing root."""

    config = load_config(write_config(git_project))
    repository = GitRepository(git_project)
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        repository.head(),
        1,
        {"public/dist/old.js": ManifestEntry("1" * 64, 3)},
    )

    plan = create_plan(config, config.target(None), repository, state, full=False)

    assert [(type(item), item.remote_path) for item in plan.operations] == [
        (DeleteOperation, "public/dist/old.js")
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

    with pytest.raises(PlanError, match="ownership conflict"):
        create_plan(config, config.target(None), GitRepository(git_project), None, full=False)


def test_complete_ownership_conflict_is_rejected_when_source_is_unchanged(
    git_project: Path,
) -> None:
    """Full ownership validation does not depend on a source operation this run."""

    generated = git_project / "generated"
    generated.mkdir()
    (generated / "app.py").write_text("generated", encoding="utf-8")
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
    repository = GitRepository(git_project)
    state = TargetState(
        1,
        "dev",
        config.target(None).fingerprint,
        repository.head(),
        1,
        {"app.py": ManifestEntry("0" * 64, 9)},
    )

    with pytest.raises(PlanError, match="ownership conflict"):
        create_plan(config, config.target(None), repository, state, full=False)


def test_ssh_alias_is_resolved_and_frozen_into_plan(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewed plans bind the effective SSH endpoint, not a movable alias string."""

    config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "sftp"
ssh_host_alias = "project-dev"
remote_root = "/srv/app"
""",
        )
    )
    endpoint = {"host": "192.0.2.10"}
    calls = 0
    real_run = subprocess.run

    def resolve(*args, **kwargs) -> subprocess.CompletedProcess[str]:  # noqa: ANN002, ANN003
        """Return a movable alias while recording resolution count."""

        if args and args[0] and args[0][0] != "ssh":
            return real_run(*args, **kwargs)
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=f"hostname {endpoint['host']}\nuser deploy\nport 2222\n",
            stderr="",
        )

    monkeypatch.setattr("git_deploy.config.subprocess.run", resolve)
    repository = GitRepository(git_project)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    endpoint["host"] = "192.0.2.99"

    assert plan.target.ssh_resolved
    assert plan.target.host == "192.0.2.10"
    assert plan.target_fingerprint == "sftp:deploy@192.0.2.10:2222:/srv/app"
    assert plan.target.fingerprint == plan.target_fingerprint
    assert calls == 1

    state = TargetState(1, "dev", plan.target_fingerprint, repository.head(), 1, {})
    with pytest.raises(PlanError, match="identity changed"):
        create_plan(config, config.target(None), repository, state, full=False)


def test_executable_source_is_preserved_for_sftp_and_rejected_for_ftp(
    git_project: Path,
) -> None:
    """Executable Git mode is explicit and FTP cannot silently drop it."""

    script = git_project / "deploy.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    commit_all(git_project, "add executable")
    repository = GitRepository(git_project)
    sftp_config = load_config(write_config(git_project))

    plan = create_plan(sftp_config, sftp_config.target(None), repository, None, full=False)
    operation = next(item for item in plan.operations if item.remote_path == "deploy.sh")
    assert isinstance(operation, UploadOperation)
    assert operation.executable

    ftp_config = load_config(
        write_config(
            git_project,
            """
[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
        )
    )
    with pytest.raises(PlanError, match="executable mode"):
        create_plan(ftp_config, ftp_config.target(None), repository, None, full=False)
