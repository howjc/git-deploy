"""Confirmed idempotent deployment application service tests."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from git_deploy.application import (
    ApplicationConfigService,
    ApplicationResult,
    ConfirmationGrant,
    DeployRequest,
    DeployService,
    PlanTokenSigner,
    ResultField,
    ResultStatus,
    RevisionPlanService,
    SideEffectLevel,
    TerminalResultEvent,
    TransactionStage,
    TransactionStageEvent,
)


def _git(path: Path, *args: str) -> str:
    """Run one Git fixture command."""

    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _planned_deploy(tmp_path: Path):
    """Create a deploy request and signed local plan for a fake executor."""

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-qm", "base")
    (repository / "app.txt").write_text("app\n", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-qm", "app")
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "deploy.example.invalid"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )
    config = ApplicationConfigService.from_path(config_path)
    selection = config.resolve_project("default", "demo")
    request = DeployRequest(
        remote="default",
        project="demo",
        side_effect=SideEffectLevel.REMOTE_MUTATION,
        expected_target_id=selection.target_id,
        expected_physical_fingerprint=selection.physical_fingerprint,
        expected_generation=None,
        revisions=("HEAD",),
    )
    signer = PlanTokenSigner(b"d" * 32)
    plan = RevisionPlanService(config, signer).plan(request)
    return signer, request, plan


def test_application_deploy_requires_token_confirmation_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Create one transaction despite a repeated execute call."""

    signer, request, plan = _planned_deploy(tmp_path)
    counters = {"transactions": 0, "writes": 0}

    def executor(_request, _plan, emit_transaction):
        """Simulate one fake-transport deployment transaction."""

        counters["transactions"] += 1
        emit_transaction("txn-1", TransactionStage.REMOTE_MUTATING)
        counters["writes"] += 1
        emit_transaction("txn-1", TransactionStage.COMMITTED)
        return ApplicationResult(
            operation=request.operation,
            remote=request.remote,
            project=request.project,
            side_effect=request.side_effect,
            status=ResultStatus.SUCCEEDED,
            summary="deployment committed",
            fields=(
                ResultField("transaction_id", "txn-1"),
                ResultField("deployment_id", "dep-1"),
                ResultField("manifest_status", "succeeded"),
            ),
        )

    events = []
    service = DeployService(signer, executor)
    with pytest.raises(PermissionError, match="confirmation"):
        service.execute(
            request,
            plan,
            token=plan.plan_token,
            confirmation=ConfirmationGrant(False),
        )
    first = service.execute(
        request,
        plan,
        token=plan.plan_token,
        confirmation=ConfirmationGrant(True),
        event_sink=events.append,
    )
    second = service.execute(
        request,
        plan,
        token=plan.plan_token,
        confirmation=ConfirmationGrant(True),
        event_sink=events.append,
    )

    assert first is second
    assert counters == {"transactions": 1, "writes": 1}
    transaction_events = [
        item for item in events if isinstance(item, TransactionStageEvent)
    ]
    assert {item.transaction_id for item in transaction_events} == {"txn-1"}
    assert isinstance(events[-1], TerminalResultEvent)
    assert events[-1].result is first


def test_application_deploy_static_noop_skips_confirmation_runs_executor(
    tmp_path: Path,
) -> None:
    """Static no-op skips confirmation but still runs executor for freshness."""

    signer, request, planned = _planned_deploy(tmp_path)
    plan = replace(
        planned,
        before_tree_id=planned.after_tree_id,
        source_changes=(),
        introduced_transition_ids=(),
        static_noop=True,
    )
    calls = {"executor": 0}

    def executor(req, reviewed, _emit):
        """Domain adapter returns already-deployed NOOP after lock-held guard."""

        calls["executor"] += 1
        return ApplicationResult(
            operation=req.operation,
            remote=req.remote,
            project=req.project,
            side_effect=req.side_effect,
            status=ResultStatus.NOOP,
            summary=(
                "No changes: target generation already matches "
                f"{reviewed.after_tree_id}"
            ),
            fields=(
                ResultField("generation", reviewed.generation),
                ResultField("target_tree_id", reviewed.after_tree_id),
            ),
        )

    events = []
    result = DeployService(signer, executor).execute(
        request,
        plan,
        token=plan.plan_token,
        confirmation=ConfirmationGrant(False),
        event_sink=events.append,
    )

    assert result.status is ResultStatus.NOOP
    assert result.summary == (
        "No changes: target generation already matches " f"{plan.after_tree_id}"
    )
    assert calls == {"executor": 1}
    assert not any(isinstance(item, TransactionStageEvent) for item in events)
    assert isinstance(events[-1], TerminalResultEvent)


def test_application_deploy_rejects_token_for_changed_request(tmp_path: Path) -> None:
    """Reject execution when request selectors differ from the reviewed plan."""

    signer, request, plan = _planned_deploy(tmp_path)
    changed = DeployRequest(
        remote=request.remote,
        project=request.project,
        side_effect=request.side_effect,
        expected_target_id=request.expected_target_id,
        expected_physical_fingerprint=request.expected_physical_fingerprint,
        expected_generation=request.expected_generation,
        revisions=("HEAD~1",),
    )

    with pytest.raises(ValueError, match="stale|reviewed"):
        DeployService(signer, lambda *_args: None).execute(  # type: ignore[arg-type]
            changed,
            plan,
            token=plan.plan_token,
            confirmation=ConfirmationGrant(True),
        )
