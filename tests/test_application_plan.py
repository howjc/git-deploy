"""Local-only application revision planning service tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.application import (
    ApplicationConfigService,
    ApplicationError,
    DeployRequest,
    PlanRequest,
    PlanTokenSigner,
    RevisionPlanService,
    RevisionSelectionOrigin,
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


def _seed_current(config: ApplicationConfigService, revision: str):
    """Create one trusted current state and its persistent Git store.

    Args:
        config: Fixture application configuration.
        revision: Known Git commit represented by current.

    Returns:
        Selected target metadata and target state root.
    """

    from git_deploy.expected_state import ExpectedStateStore, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.state_bootstrap import StateBootstrapService
    from git_deploy.target_identity import default_state_base

    selection = config.resolve_project("dev", "demo")
    _alias, _server, project, identity = config._resolve_domain_project("dev", "demo")
    target_root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    plan = StateBootstrapService(project, identity, target_root).plan_inferred(revision)
    git_store = PersistentGitStore(target_root, project.repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=plan.source_tree_id,
        applied_transition_ids=plan.applied_transition_ids,
        physical_fingerprint=selection.physical_fingerprint,
        policy_fingerprint=selection.policy_fingerprint,
    )
    ExpectedStateStore(target_root, identity).cas_advance(
        expected_generation=None,
        state=state,
    )
    return selection, target_root


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


def test_implicit_plan_freezes_current_to_head_in_digest_and_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind an implicit plan to missing commits even if HEAD later moves."""

    config, head, _state_dir = _fixture(tmp_path)
    repository = config._resolve_domain_project("dev", "demo")[2].repository
    baseline = _git(repository, "rev-parse", f"{head}^1")
    selection, _target_root = _seed_current(config, baseline)

    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.worktree import WorktreeManager

    def forbidden(*_args, **_kwargs):
        """Fail when local-only implicit planning attempts a write."""

        raise AssertionError("implicit plan attempted a durable write")

    monkeypatch.setattr(ExpectedStateStore, "write_state", forbidden)
    monkeypatch.setattr(ExpectedStateStore, "cas_advance", forbidden)
    monkeypatch.setattr(WorktreeManager, "materialize", forbidden)

    signer = PlanTokenSigner(b"i" * 32)
    service = RevisionPlanService(config, signer)
    request = PlanRequest(
        remote="dev",
        project="demo",
        side_effect=SideEffectLevel.LOCAL_READ,
        expected_target_id=selection.target_id,
        expected_physical_fingerprint=selection.physical_fingerprint,
        expected_generation=1,
        revisions=(),
    )
    result = service.plan(request)
    deploy_preview = service.plan(
        DeployRequest(
            remote="dev",
            project="demo",
            side_effect=SideEffectLevel.LOCAL_READ,
            expected_target_id=selection.target_id,
            expected_physical_fingerprint=selection.physical_fingerprint,
            expected_generation=1,
            revisions=(),
            dry_run=True,
        )
    )

    assert result.selection_origin is RevisionSelectionOrigin.IMPLICIT_CURRENT_TO_HEAD
    assert result.resolved_revisions == (head,)
    assert result.before_tree_id == _git(repository, "rev-parse", f"{baseline}^{{tree}}")
    assert result.after_tree_id == _git(repository, "rev-parse", f"{head}^{{tree}}")
    assert [item.path for item in result.source_changes] == ["app.txt"]
    assert deploy_preview.plan_digest == result.plan_digest
    assert deploy_preview.before_tree_id == result.before_tree_id
    assert deploy_preview.after_tree_id == result.after_tree_id
    assert deploy_preview.source_changes == result.source_changes
    signer.verify(
        result.plan_token,
        request,
        policy_fingerprint=selection.policy_fingerprint,
        plan_digest=result.plan_digest,
    )

    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repository, "add", "later.txt")
    _git(repository, "commit", "-qm", "later")
    moved_head = _git(repository, "rev-parse", "HEAD")
    moved = service.plan(request)

    assert result.resolved_revisions == (head,)
    signer.verify(
        result.plan_token,
        request,
        policy_fingerprint=selection.policy_fingerprint,
        plan_digest=result.plan_digest,
    )
    assert moved.resolved_revisions == (head, moved_head)
    assert moved.plan_digest != result.plan_digest


def test_implicit_plan_is_noop_when_current_matches_head(tmp_path: Path) -> None:
    """Return a zero-write static no-op when every HEAD transition is applied."""

    config, head, _state_dir = _fixture(tmp_path)
    selection, _target_root = _seed_current(config, head)

    result = RevisionPlanService(config, PlanTokenSigner(b"n" * 32)).plan(
        PlanRequest(
            remote="dev",
            project="demo",
            side_effect=SideEffectLevel.LOCAL_READ,
            expected_target_id=selection.target_id,
            expected_physical_fingerprint=selection.physical_fingerprint,
            expected_generation=1,
            revisions=(),
        )
    )

    assert result.selection_origin is RevisionSelectionOrigin.IMPLICIT_CURRENT_TO_HEAD
    assert result.resolved_revisions == ()
    assert result.static_noop is True
    assert result.source_changes == ()
    assert result.before_tree_id == result.after_tree_id


def test_implicit_plan_requires_trusted_current_state(tmp_path: Path) -> None:
    """Refuse implicit selection instead of guessing a first deployment baseline."""

    config, _head, _state_dir = _fixture(tmp_path)
    selection = config.resolve_project("dev", "demo")

    with pytest.raises(ApplicationError) as captured:
        RevisionPlanService(config, PlanTokenSigner(b"m" * 32)).plan(
            PlanRequest(
                remote="dev",
                project="demo",
                side_effect=SideEffectLevel.LOCAL_READ,
                expected_target_id=selection.target_id,
                expected_physical_fingerprint=selection.physical_fingerprint,
                expected_generation=None,
                revisions=(),
            )
        )

    error = captured.value
    serialized = error.to_dict()
    assert error.code == "state.current-missing"
    assert serialized["context"] == {
        "bootstrap_empty_command": (
            "git-deploy state bootstrap demo --empty --remote dev --yes"
        ),
        "bootstrap_revision_command": (
            "git-deploy state bootstrap demo --revision COMMIT --remote dev --yes"
        ),
        "project": "demo",
        "remote": "dev",
    }
    assert "artifact state requirements" in error.message
    assert "password" not in repr(serialized).lower()
