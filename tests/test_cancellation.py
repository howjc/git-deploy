"""Coordinated application cancellation state machine tests."""

from __future__ import annotations

import pytest

from git_deploy.application import (
    CancellationAction,
    CancellationState,
    CancellationStateMachine,
    TransactionStage,
)


@pytest.mark.parametrize("stage", [None, TransactionStage.PREPARING])
def test_state_machine_cancels_immediately_before_remote_mutation(
    stage: TransactionStage | None,
) -> None:
    """Allow immediate cancellation while no remote bytes can have changed."""

    machine = CancellationStateMachine()
    if stage is not None:
        machine.advance(stage)

    transition = machine.request_cancel()

    assert transition.action is CancellationAction.CANCEL_IMMEDIATELY
    assert transition.state is CancellationState.CANCELLED


@pytest.mark.parametrize(
    "stage",
    [
        TransactionStage.REMOTE_MUTATING,
        TransactionStage.VERIFYING,
        TransactionStage.RUNNING_HOOKS,
        TransactionStage.COMMITTING,
        TransactionStage.ROLLING_BACK,
    ],
)
def test_state_machine_waits_for_executor_after_remote_mutation(
    stage: TransactionStage,
) -> None:
    """Never let UI cancellation claim that an in-flight mutation rolled back."""

    machine = CancellationStateMachine()
    machine.advance(stage)

    transition = machine.request_cancel()

    assert transition.action is CancellationAction.WAIT_FOR_EXECUTOR
    assert transition.state is CancellationState.COORDINATION_REQUESTED
    assert transition.transaction_stage is stage


def test_state_machine_distinguishes_committed_and_recovered_outcomes() -> None:
    """Keep successful commit distinct from executor-confirmed recovery."""

    committed = CancellationStateMachine()
    committed.advance(TransactionStage.COMMITTED)
    recovered = CancellationStateMachine()
    recovered.advance(TransactionStage.ROLLED_BACK)

    assert committed.request_cancel().action is CancellationAction.ALREADY_COMMITTED
    assert committed.state is CancellationState.COMMITTED
    assert recovered.request_cancel().action is CancellationAction.ALREADY_CANCELLED
    assert recovered.state is CancellationState.RECOVERED


def test_state_machine_preserves_manual_recovery_requirement() -> None:
    """Expose uncertain remote state instead of reporting cancellation success."""

    machine = CancellationStateMachine()
    machine.advance(TransactionStage.REMOTE_MUTATING)
    machine.request_cancel()
    machine.advance(TransactionStage.RECOVERY_REQUIRED)

    transition = machine.request_cancel()

    assert transition.action is CancellationAction.MANUAL_RECOVERY_REQUIRED
    assert transition.state is CancellationState.MANUAL_RECOVERY_REQUIRED


def test_state_machine_ui_close_is_only_a_cancellation_request() -> None:
    """Model close/Esc/Ctrl-C without inventing a successful remote rollback."""

    machine = CancellationStateMachine()
    machine.advance(TransactionStage.REMOTE_MUTATING)

    close_result = machine.request_cancel()

    assert close_result.state is CancellationState.COORDINATION_REQUESTED
    assert close_result.state is not CancellationState.RECOVERED
