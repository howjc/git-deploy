"""Latest rollback application service tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_deploy.application import (
    ApplicationConfigService,
    ApplicationResult,
    ConfirmationGrant,
    LatestRollbackService,
    PlanTokenSigner,
    ResultField,
    ResultStatus,
    RollbackRequest,
    SideEffectLevel,
    TerminalResultEvent,
    TransactionStage,
)
from git_deploy.expected_state import ExpectedStateStore, build_expected_state
from git_deploy.models import DeploymentManifest, FileSnapshot
from git_deploy.state import DeploymentStore
from git_deploy.target_identity import default_state_base


def _latest_fixture(tmp_path: Path):
    """Create current generation and latest successful deployment manifest."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "rollback.example.invalid"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = "{tmp_path / 'state'}"
""".strip(),
        encoding="utf-8",
    )
    config = ApplicationConfigService.from_path(config_path)
    selection = config.resolve_project("default", "demo")
    _alias, _server, project, identity = config._resolve_domain_project("default", "demo")
    target_root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    store = ExpectedStateStore(target_root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree-current",
        applied_transition_ids=(),
        physical_fingerprint=selection.physical_fingerprint,
        policy_fingerprint=selection.policy_fingerprint,
    )
    store.cas_advance(expected_generation=None, state=state)
    before = b"before\n"
    after = b"after\n"
    DeploymentStore(project, root=target_root).write_manifest(
        DeploymentManifest(
            deployment_id="20260713-latest",
            project="demo",
            repository=str(repository),
            remote_root="/srv/demo",
            created_at="2026-07-13T00:00:00Z",
            status="succeeded",
            from_commit="a",
            to_commit="b",
            snapshots=[
                FileSnapshot(
                    path="app.txt",
                    remote_path="/srv/demo/app.txt",
                    before_exists=True,
                    before_sha256=hashlib.sha256(before).hexdigest(),
                    backup_file="backups/00000.bin",
                    after_exists=True,
                    after_sha256=hashlib.sha256(after).hexdigest(),
                )
            ],
            before_generation=0,
            after_generation=1,
            state="v1",
        )
    )
    request = RollbackRequest(
        remote="default",
        project="demo",
        side_effect=SideEffectLevel.REMOTE_MUTATION,
        expected_target_id=selection.target_id,
        expected_physical_fingerprint=selection.physical_fingerprint,
        expected_generation=1,
        latest=True,
    )
    return config, request


def test_latest_rollback_preview_execute_events_and_derived_state(
    tmp_path: Path,
) -> None:
    """Separate preview from execution and return the derived generation."""

    config, request = _latest_fixture(tmp_path)
    signer = PlanTokenSigner(b"r" * 32)
    counters = {"transactions": 0, "writes": 0}

    def executor(_request, plan, emit_transaction):
        """Simulate a stateful latest rollback over a fake transport."""

        counters["transactions"] += 1
        emit_transaction("txn-rollback", TransactionStage.REMOTE_MUTATING)
        counters["writes"] += 1
        emit_transaction("txn-rollback", TransactionStage.COMMITTED)
        return ApplicationResult(
            operation=request.operation,
            remote=request.remote,
            project=request.project,
            side_effect=request.side_effect,
            status=ResultStatus.SUCCEEDED,
            summary="rollback committed",
            fields=(
                ResultField("transaction_id", "txn-rollback"),
                ResultField("deployment_id", plan.deployment_id),
                ResultField("generation", 2),
            ),
        )

    events = []
    service = LatestRollbackService(config, signer, executor)
    plan = service.preview(request)
    assert plan.paths[0].action == "restore"
    assert counters == {"transactions": 0, "writes": 0}

    with pytest.raises(PermissionError, match="confirmation"):
        service.execute(
            request,
            plan,
            token=plan.plan_token,
            confirmation=ConfirmationGrant(False),
        )
    result = service.execute(
        request,
        plan,
        token=plan.plan_token,
        confirmation=ConfirmationGrant(True),
        event_sink=events.append,
    )
    repeated = service.execute(
        request,
        plan,
        token=plan.plan_token,
        confirmation=ConfirmationGrant(True),
    )

    assert result is repeated
    assert counters == {"transactions": 1, "writes": 1}
    assert isinstance(events[-1], TerminalResultEvent)
    assert events[-1].result is result
