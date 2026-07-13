"""Progress event terminal rendering tests."""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError

import pytest

from git_deploy.application import (
    ApplicationResult,
    OperationEventKind,
    OperationKind,
    OperationProgressEvent,
    OperationStartedEvent,
    OperationWarningEvent,
    ResultStatus,
    SideEffectLevel,
    TargetResolvedEvent,
    TerminalResultEvent,
    TransactionStage,
    TransactionStageEvent,
)
from git_deploy.progress import ProgressEvent, TerminalProgress


def test_non_tty_progress_reports_start_and_completion() -> None:
    """Keep redirected logs informative without printing every byte chunk."""

    stream = io.StringIO()
    renderer = TerminalProgress("demo", stream=stream)

    renderer.update(ProgressEvent("upload", 0, 2, bytes_total=2048))
    renderer.update(
        ProgressEvent(
            "upload",
            0,
            2,
            path="first.bin",
            bytes_completed=1024,
            bytes_total=2048,
        )
    )
    renderer.update(
        ProgressEvent(
            "upload",
            2,
            2,
            path="second.bin",
            bytes_completed=2048,
            bytes_total=2048,
        )
    )
    renderer.finish()
    output = stream.getvalue()

    assert "[demo]" in output
    assert "UPLOAD" in output
    assert "0/2" in output
    assert "2/2" in output
    assert "2.0 KiB/2.0 KiB" in output


def test_operation_event_contract_covers_context_progress_and_terminal_result() -> None:
    """Expose typed immutable events for every adapter-visible event family."""

    context = {
        "operation": OperationKind.DEPLOY,
        "remote": "prod",
        "project": "application",
    }
    result = ApplicationResult(
        operation=OperationKind.DEPLOY,
        remote="prod",
        project="application",
        side_effect=SideEffectLevel.REMOTE_MUTATION,
        status=ResultStatus.SUCCEEDED,
        summary="deployment committed",
    )
    events = (
        OperationStartedEvent(
            **context,
            sequence=0,
            side_effect=SideEffectLevel.REMOTE_MUTATION,
        ),
        TargetResolvedEvent(
            **context,
            sequence=1,
            target_id="tgt-example",
            physical_fingerprint="a" * 64,
            generation=7,
        ),
        OperationWarningEvent(
            **context,
            sequence=2,
            code="build.host-trust",
            message="host build runs with current user permissions",
        ),
        OperationProgressEvent(
            **context,
            sequence=3,
            progress=ProgressEvent("upload", 1, 2, path="public/app.js"),
        ),
        TransactionStageEvent(
            **context,
            sequence=4,
            transaction_id="txn-example",
            stage=TransactionStage.REMOTE_MUTATING,
        ),
        TerminalResultEvent(
            **context,
            sequence=5,
            result=result,
        ),
    )

    assert tuple(event.kind for event in events) == tuple(OperationEventKind)
    assert events[3].progress.phase == "upload"
    assert events[5].result is result
    with pytest.raises(FrozenInstanceError):
        events[0].sequence = 9  # type: ignore[misc]


def test_operation_event_contract_rejects_malformed_or_mismatched_payloads() -> None:
    """Reject ambiguous counters, warning codes, and terminal result contexts."""

    context = {
        "operation": OperationKind.PLAN,
        "remote": "dev",
        "project": "application",
        "sequence": 0,
    }
    with pytest.raises(ValueError, match="completed cannot exceed total"):
        ProgressEvent("upload", 2, 1)
    with pytest.raises(ValueError, match="stable lowercase"):
        OperationWarningEvent(**context, code="Bad warning", message="unsafe")
    with pytest.raises(ValueError, match="must match"):
        TerminalResultEvent(
            **context,
            result=ApplicationResult(
                operation=OperationKind.PLAN,
                remote="prod",
                project="application",
                side_effect=SideEffectLevel.LOCAL_READ,
                status=ResultStatus.PLANNED,
                summary="planned",
            ),
        )
