"""Background application worker ordering and exclusion tests."""

from __future__ import annotations

import threading

import pytest

from git_deploy.application import (
    ApplicationResult,
    ApplicationWorker,
    DuplicateOperationError,
    OperationKind,
    OperationStartedEvent,
    ResultStatus,
    SideEffectLevel,
    TargetBusyError,
    TerminalResultEvent,
)


def _result() -> ApplicationResult:
    """Return a stable successful worker result."""

    return ApplicationResult(
        operation=OperationKind.DEPLOY,
        remote="dev",
        project="demo",
        side_effect=SideEffectLevel.REMOTE_MUTATION,
        status=ResultStatus.SUCCEEDED,
        summary="done",
    )


def test_application_worker_orders_events_and_rejects_target_conflicts() -> None:
    """Keep one mutation per target while a synchronous task runs off-thread."""

    worker = ApplicationWorker(max_workers=2)
    started = threading.Event()
    release = threading.Event()
    events = []

    def task(emit):
        """Block one worker while exposing an ordered event stream."""

        emit(
            OperationStartedEvent(
                operation=OperationKind.DEPLOY,
                remote="dev",
                project="demo",
                sequence=0,
                side_effect=SideEffectLevel.REMOTE_MUTATION,
            )
        )
        started.set()
        assert release.wait(timeout=5)
        result = _result()
        emit(
            TerminalResultEvent(
                operation=OperationKind.DEPLOY,
                remote="dev",
                project="demo",
                sequence=1,
                result=result,
            )
        )
        return result

    try:
        handle = worker.submit(
            operation_id="op-1",
            target_id="tgt-1",
            mutation=True,
            task=task,
            event_sink=events.append,
        )
        assert started.wait(timeout=5)
        with pytest.raises(TargetBusyError, match="active mutation"):
            worker.submit(
                operation_id="op-2",
                target_id="tgt-1",
                mutation=True,
                task=task,
            )
        with pytest.raises(DuplicateOperationError, match="already submitted"):
            worker.submit(
                operation_id="op-1",
                target_id="tgt-2",
                mutation=True,
                task=task,
            )
        release.set()
        assert handle.future.result(timeout=5).status is ResultStatus.SUCCEEDED
        assert [event.sequence for event in events] == [0, 1]
    finally:
        release.set()
        worker.shutdown()


def test_application_worker_rejects_out_of_order_events() -> None:
    """Fail a worker operation when a service violates event sequence ordering."""

    worker = ApplicationWorker(max_workers=1)

    def task(emit):
        """Emit an invalid first sequence for the ordering gate."""

        emit(
            OperationStartedEvent(
                operation=OperationKind.DEPLOY,
                remote="dev",
                project="demo",
                sequence=1,
                side_effect=SideEffectLevel.REMOTE_MUTATION,
            )
        )
        return _result()

    try:
        handle = worker.submit(
            operation_id="op-bad",
            target_id="tgt-1",
            mutation=True,
            task=task,
        )
        with pytest.raises(ValueError, match="out of order"):
            handle.future.result(timeout=5)
    finally:
        worker.shutdown()
