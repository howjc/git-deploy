"""Background worker adapter for synchronous application services."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .cancellation import CancellationStateMachine, CancellationTransition
from .events import ApplicationEvent, TransactionStageEvent
from .results import ApplicationResult


class DuplicateOperationError(RuntimeError):
    """Raised when an operation ID is submitted more than once."""


class TargetBusyError(RuntimeError):
    """Raised when a target already has an active mutation worker."""


WorkerEventSink = Callable[[ApplicationEvent], None]
WorkerTask = Callable[[WorkerEventSink], ApplicationResult]


@dataclass(slots=True)
class WorkerHandle:
    """Submitted operation future and coordinated cancellation controller."""

    operation_id: str
    target_id: str
    mutation: bool
    future: Future[ApplicationResult]
    cancellation: CancellationStateMachine

    def request_cancel(self) -> CancellationTransition:
        """Request cancellation without claiming remote rollback success."""

        transition = self.cancellation.request_cancel()
        if transition.action.value == "cancel_immediately":
            self.future.cancel()
        return transition


class ApplicationWorker:
    """Run synchronous services off-loop with target mutation exclusion."""

    def __init__(self, *, max_workers: int = 4):
        """Create a bounded worker pool.

        Args:
            max_workers: Positive maximum number of simultaneous tasks.
        """

        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="git-deploy-app",
        )
        self._lock = threading.Lock()
        self._submitted_ids: set[str] = set()
        self._active_targets: dict[str, str] = {}

    def submit(
        self,
        *,
        operation_id: str,
        target_id: str,
        mutation: bool,
        task: WorkerTask,
        event_sink: WorkerEventSink | None = None,
    ) -> WorkerHandle:
        """Submit one synchronous application operation.

        Args:
            operation_id: Caller-owned unique operation identifier.
            target_id: Physical target identifier used for mutation exclusion.
            mutation: Whether this task can mutate local/remote target state.
            task: Synchronous service callback receiving an ordered event sink.
            event_sink: Optional adapter event consumer.

        Returns:
            Handle exposing the future and coordinated cancellation state.
        """

        for name, value in (("operation_id", operation_id), ("target_id", target_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(mutation, bool):
            raise TypeError("mutation must be a bool")
        if not callable(task):
            raise TypeError("task must be callable")

        with self._lock:
            if operation_id in self._submitted_ids:
                raise DuplicateOperationError(
                    f"operation {operation_id!r} was already submitted"
                )
            active = self._active_targets.get(target_id)
            if mutation and active is not None:
                raise TargetBusyError(
                    f"target {target_id!r} already has active mutation {active!r}"
                )
            self._submitted_ids.add(operation_id)
            if mutation:
                self._active_targets[target_id] = operation_id

        cancellation = CancellationStateMachine()
        next_sequence = 0
        event_lock = threading.Lock()

        def ordered_sink(event: ApplicationEvent) -> None:
            """Validate ordered delivery and update cancellation transaction state."""

            nonlocal next_sequence
            with event_lock:
                if event.sequence != next_sequence:
                    raise ValueError(
                        "application event sequence out of order: "
                        f"expected {next_sequence}, got {event.sequence}"
                    )
                next_sequence += 1
                if isinstance(event, TransactionStageEvent):
                    cancellation.advance(event.stage)
                if event_sink is not None:
                    event_sink(event)

        try:
            future = self._executor.submit(task, ordered_sink)
        except BaseException:
            self._release_target(target_id, operation_id, mutation)
            raise
        handle = WorkerHandle(
            operation_id=operation_id,
            target_id=target_id,
            mutation=mutation,
            future=future,
            cancellation=cancellation,
        )
        future.add_done_callback(
            lambda _future: self._release_target(target_id, operation_id, mutation)
        )
        return handle

    def shutdown(self, *, wait: bool = True) -> None:
        """Release worker threads after submitted operations finish or cancel.

        Args:
            wait: Wait for running operations when true.
        """

        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _release_target(
        self,
        target_id: str,
        operation_id: str,
        mutation: bool,
    ) -> None:
        """Release a target reservation owned by one completed mutation."""

        if not mutation:
            return
        with self._lock:
            if self._active_targets.get(target_id) == operation_id:
                self._active_targets.pop(target_id, None)
