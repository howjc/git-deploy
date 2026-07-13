"""Coordinated cancellation state machine for application workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .events import TransactionStage


class CancellationState(StrEnum):
    """Stable worker cancellation states visible to adapters."""

    RUNNING = "running"
    CANCELLED = "cancelled"
    COORDINATION_REQUESTED = "coordination_requested"
    COMMITTED = "committed"
    RECOVERED = "recovered"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


class CancellationAction(StrEnum):
    """Action an adapter should take after requesting cancellation."""

    CANCEL_IMMEDIATELY = "cancel_immediately"
    WAIT_FOR_EXECUTOR = "wait_for_executor"
    ALREADY_COMMITTED = "already_committed"
    ALREADY_CANCELLED = "already_cancelled"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


@dataclass(frozen=True, slots=True)
class CancellationTransition:
    """One immutable cancellation state transition returned to an adapter."""

    state: CancellationState
    action: CancellationAction
    transaction_stage: TransactionStage | None


class CancellationStateMachine:
    """Track cancellation without treating UI closure as remote rollback."""

    def __init__(self) -> None:
        """Initialize a cancellable operation before transaction mutation."""

        self._state = CancellationState.RUNNING
        self._transaction_stage: TransactionStage | None = None

    @property
    def state(self) -> CancellationState:
        """Return the current stable cancellation state."""

        return self._state

    @property
    def transaction_stage(self) -> TransactionStage | None:
        """Return the latest executor-reported transaction stage."""

        return self._transaction_stage

    def advance(self, stage: TransactionStage) -> CancellationState:
        """Apply one executor-owned transaction stage transition.

        Args:
            stage: Durable transaction stage reported by the executor.

        Returns:
            Updated stable cancellation state.
        """

        if not isinstance(stage, TransactionStage):
            raise TypeError("stage must be a TransactionStage")
        if self._state in {
            CancellationState.COMMITTED,
            CancellationState.RECOVERED,
            CancellationState.MANUAL_RECOVERY_REQUIRED,
        }:
            if stage is not self._transaction_stage:
                raise ValueError("terminal cancellation state cannot transition again")
            return self._state

        self._transaction_stage = stage
        if stage is TransactionStage.COMMITTED:
            self._state = CancellationState.COMMITTED
        elif stage is TransactionStage.ROLLED_BACK:
            self._state = CancellationState.RECOVERED
        elif stage is TransactionStage.RECOVERY_REQUIRED:
            self._state = CancellationState.MANUAL_RECOVERY_REQUIRED
        return self._state

    def request_cancel(self) -> CancellationTransition:
        """Request cancellation according to the current durable stage.

        Returns:
            Adapter action and resulting stable state. Remote mutation stages
            always require executor coordination rather than UI-side rollback.
        """

        if self._state is CancellationState.CANCELLED:
            action = CancellationAction.ALREADY_CANCELLED
        elif self._state is CancellationState.COMMITTED:
            action = CancellationAction.ALREADY_COMMITTED
        elif self._state is CancellationState.MANUAL_RECOVERY_REQUIRED:
            action = CancellationAction.MANUAL_RECOVERY_REQUIRED
        elif self._state is CancellationState.RECOVERED:
            action = CancellationAction.ALREADY_CANCELLED
        elif self._requires_coordination():
            self._state = CancellationState.COORDINATION_REQUESTED
            action = CancellationAction.WAIT_FOR_EXECUTOR
        else:
            self._state = CancellationState.CANCELLED
            action = CancellationAction.CANCEL_IMMEDIATELY
        return CancellationTransition(self._state, action, self._transaction_stage)

    def _requires_coordination(self) -> bool:
        """Return whether the executor may already have changed remote state."""

        return self._transaction_stage in {
            TransactionStage.REMOTE_MUTATING,
            TransactionStage.VERIFYING,
            TransactionStage.RUNNING_HOOKS,
            TransactionStage.COMMITTING,
            TransactionStage.ROLLING_BACK,
        }
