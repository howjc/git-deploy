"""Immutable operation events shared by CLI and future TUI adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeAlias

from git_deploy.progress import ProgressEvent

from .models import OperationKind, SideEffectLevel
from .results import ApplicationResult


class OperationEventKind(StrEnum):
    """Stable discriminator values for application operation events."""

    OPERATION = "operation"
    TARGET = "target"
    WARNING = "warning"
    PROGRESS = "progress"
    TRANSACTION = "transaction"
    TERMINAL = "terminal"


class TransactionStage(StrEnum):
    """Stable transaction stages visible to adapters and operators."""

    PREPARING = "preparing"
    REMOTE_MUTATING = "remote_mutating"
    VERIFYING = "verifying"
    RUNNING_HOOKS = "running_hooks"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationEvent:
    """Common immutable context carried by every application event.

    Args:
        operation: Operation family that emitted the event.
        remote: Explicit selected remote alias.
        project: Explicit selected project key.
        sequence: Zero-based monotonic sequence within one operation.
    """

    kind: ClassVar[OperationEventKind]
    operation: OperationKind
    remote: str
    project: str
    sequence: int

    def __post_init__(self) -> None:
        """Validate adapter-neutral operation context."""

        if not isinstance(self.operation, OperationKind):
            raise TypeError("operation must be an OperationKind")
        for name in ("remote", "project"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationStartedEvent(OperationEvent):
    """Announce the operation and its maximum authorized side effect."""

    kind: ClassVar[OperationEventKind] = OperationEventKind.OPERATION
    side_effect: SideEffectLevel

    def __post_init__(self) -> None:
        """Validate the declared side-effect level."""

        super(OperationStartedEvent, self).__post_init__()
        if not isinstance(self.side_effect, SideEffectLevel):
            raise TypeError("side_effect must be a SideEffectLevel")


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetResolvedEvent(OperationEvent):
    """Describe the physical target and generation selected for execution."""

    kind: ClassVar[OperationEventKind] = OperationEventKind.TARGET
    target_id: str
    physical_fingerprint: str
    generation: int | None

    def __post_init__(self) -> None:
        """Validate explicit target identity without inferring from aliases."""

        super(TargetResolvedEvent, self).__post_init__()
        for name in ("target_id", "physical_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.generation is not None and (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("generation must be a non-negative integer or None")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationWarningEvent(OperationEvent):
    """Expose one stable, renderer-neutral operation warning."""

    kind: ClassVar[OperationEventKind] = OperationEventKind.WARNING
    code: str
    message: str

    def __post_init__(self) -> None:
        """Validate stable warning identifiers and non-empty messages."""

        super(OperationWarningEvent, self).__post_init__()
        if not isinstance(self.code, str) or not _WARNING_CODE.fullmatch(self.code):
            raise ValueError("warning code must be a stable lowercase dotted identifier")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("warning message must be a non-empty string")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationProgressEvent(OperationEvent):
    """Wrap a legacy executor progress snapshot in operation context."""

    kind: ClassVar[OperationEventKind] = OperationEventKind.PROGRESS
    progress: ProgressEvent

    def __post_init__(self) -> None:
        """Require the backward-compatible progress payload."""

        super(OperationProgressEvent, self).__post_init__()
        if not isinstance(self.progress, ProgressEvent):
            raise TypeError("progress must be a ProgressEvent")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransactionStageEvent(OperationEvent):
    """Report one durable transaction stage transition."""

    kind: ClassVar[OperationEventKind] = OperationEventKind.TRANSACTION
    transaction_id: str
    stage: TransactionStage

    def __post_init__(self) -> None:
        """Validate transaction identity and stage."""

        super(TransactionStageEvent, self).__post_init__()
        if not isinstance(self.transaction_id, str) or not self.transaction_id.strip():
            raise ValueError("transaction_id must be a non-empty string")
        if not isinstance(self.stage, TransactionStage):
            raise TypeError("stage must be a TransactionStage")


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminalResultEvent(OperationEvent):
    """Publish the final structured result for an operation."""

    kind: ClassVar[OperationEventKind] = OperationEventKind.TERMINAL
    result: ApplicationResult

    def __post_init__(self) -> None:
        """Ensure the terminal result belongs to the same operation context."""

        super(TerminalResultEvent, self).__post_init__()
        if not isinstance(self.result, ApplicationResult):
            raise TypeError("result must be an ApplicationResult")
        if (
            self.result.operation is not self.operation
            or self.result.remote != self.remote
            or self.result.project != self.project
        ):
            raise ValueError("terminal result must match its operation context")


ApplicationEvent: TypeAlias = (
    OperationStartedEvent
    | TargetResolvedEvent
    | OperationWarningEvent
    | OperationProgressEvent
    | TransactionStageEvent
    | TerminalResultEvent
)


_WARNING_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
