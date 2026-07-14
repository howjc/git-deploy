"""Immutable structured application results with no renderer dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from .models import OperationKind, SideEffectLevel

ResultScalar: TypeAlias = str | int | float | bool | None
ResultValue: TypeAlias = ResultScalar | tuple[ResultScalar, ...]


class ResultStatus(StrEnum):
    """Stable terminal or preview status values returned by services."""

    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    NOOP = "noop"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ResultField:
    """One renderer-neutral named result value."""

    name: str
    value: ResultValue

    def __post_init__(self) -> None:
        """Validate the field name and freeze sequence values."""

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("result field name must be a non-empty string")
        object.__setattr__(self, "value", _freeze_result_value(self.value))


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationResult:
    """Structured result consumed independently by CLI and TUI renderers.

    Args:
        operation: Stable operation identifier.
        remote: Selected remote alias.
        project: Selected project key.
        side_effect: Side-effect level actually used.
        status: Stable result status.
        summary: Human-readable non-secret summary.
        fields: Renderer-neutral structured values.
        warnings: Non-secret warning messages.
    """

    operation: OperationKind
    remote: str
    project: str
    side_effect: SideEffectLevel
    status: ResultStatus
    summary: str
    fields: tuple[ResultField, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Freeze collections and reject arbitrary renderer/widget objects."""

        if not isinstance(self.operation, OperationKind):
            raise TypeError("operation must be an OperationKind")
        if not isinstance(self.side_effect, SideEffectLevel):
            raise TypeError("side_effect must be a SideEffectLevel")
        if not isinstance(self.status, ResultStatus):
            raise TypeError("status must be a ResultStatus")
        for name in ("remote", "project", "summary"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        fields = tuple(self.fields)
        if any(not isinstance(field, ResultField) for field in fields):
            raise TypeError("fields must contain only ResultField values")
        warnings = tuple(self.warnings)
        if any(not isinstance(item, str) or not item.strip() for item in warnings):
            raise TypeError("warnings must contain non-empty strings")
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "warnings", warnings)


def _freeze_result_value(value: object) -> ResultValue:
    """Convert a result value to the supported immutable value grammar.

    Args:
        value: Candidate scalar or flat scalar sequence.

    Returns:
        Immutable scalar or tuple.

    Raises:
        TypeError: If the value could retain a renderer or arbitrary object.
    """

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_result_scalar(item) for item in value)
    raise TypeError(
        "result values must be scalars or flat scalar sequences; "
        "renderer/widget objects are not allowed"
    )


def _freeze_result_scalar(value: object) -> ResultScalar:
    """Validate and return one renderer-neutral result scalar.

    Args:
        value: Candidate scalar value.

    Returns:
        The validated scalar.

    Raises:
        TypeError: If the value is not part of the result scalar grammar.
    """

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    raise TypeError(
        "result values must be scalars or flat scalar sequences; "
        "renderer/widget objects are not allowed"
    )
