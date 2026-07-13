"""Local-only application revision planning service tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from git_deploy.application import (
    ApplicationConfigService,
    PlanRequest,
    PlanTokenSigner,
    RevisionPlanService,
    SideEffectLevel,
)


def _git(path: Path, *args: str) -> str:
    """Run a Git fixture command and return stripped stdout."""

    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(tmp_path: Path) -> tuple[ApplicationConfigService, str, Path]:
    """Create a two-commit repository and build-enabled configuration."""

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-qm", "base")
    (repository / "app.txt").write_text("application\n", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-qm", "application")
    head = _git(repository, "rev-parse", "HEAD")

    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"
risk = "standard"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = "{tmp_path / 'state'}"

[projects.demo.build]
runner = "host"
commands = [["true"]]

[[projects.demo.artifacts]]
source = "dist"
destination = "public/dist"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    return ApplicationConfigService.from_path(config_path), head, tmp_path / "state"


def test_application_plan_is_structured_and_strictly_local_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Return source/artifact/build/warnings without remote or durable writes."""

    config, head, state_dir = _fixture(tmp_path)
    selection = config.resolve_project("dev", "demo")

    def forbidden(*_args, **_kwargs):
        """Fail if a local-only plan reaches a side-effecting component."""

        raise AssertionError("local-only plan invoked a forbidden side effect")

    from git_deploy.build_service import BuildService
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.worktree import WorktreeManager

    monkeypatch.setattr(BuildService, "execute", forbidden)
    monkeypatch.setattr(WorktreeManager, "materialize", forbidden)
    monkeypatch.setattr(ExpectedStateStore, "write_state", forbidden)
    monkeypatch.setattr(ExpectedStateStore, "cas_advance", forbidden)

    service = RevisionPlanService(config, PlanTokenSigner(b"s" * 32))
    result = service.plan(
        PlanRequest(
            remote="dev",
            project="demo",
            side_effect=SideEffectLevel.LOCAL_READ,
            expected_target_id=selection.target_id,
            expected_physical_fingerprint=selection.physical_fingerprint,
            expected_generation=None,
            revisions=(head,),
        )
    )

    assert [(item.action, item.path) for item in result.source_changes] == [
        ("upload", "app.txt")
    ]
    assert result.artifacts[0].destination == "public/dist"
    assert result.artifacts[0].status == "deferred_until_build"
    assert result.build.enabled is True
    assert result.build.runner == "host"
    assert result.warnings
    assert result.remote_verified is False
    assert len(result.plan_digest) == 64
    assert str(result.plan_token).startswith("v1.")
    assert not state_dir.exists()
