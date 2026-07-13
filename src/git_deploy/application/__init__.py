"""UI-neutral application contracts shared by CLI and future TUI adapters."""

from .models import (
    ApplicationRequest,
    DeployRequest,
    GCRequest,
    HistoryRequest,
    OperationKind,
    OperationRequest,
    PlanRequest,
    RollbackRequest,
    SideEffectLevel,
    StateAction,
    StateRequest,
    VerifyRequest,
)
from .errors import (
    ApplicationError,
    ErrorCategory,
    ErrorContextItem,
    application_error_from_exception,
)
from .results import ApplicationResult, ResultField, ResultStatus

__all__ = [
    "ApplicationRequest",
    "ApplicationError",
    "ApplicationResult",
    "DeployRequest",
    "ErrorCategory",
    "ErrorContextItem",
    "GCRequest",
    "HistoryRequest",
    "OperationKind",
    "OperationRequest",
    "PlanRequest",
    "ResultField",
    "ResultStatus",
    "RollbackRequest",
    "SideEffectLevel",
    "StateAction",
    "StateRequest",
    "VerifyRequest",
    "application_error_from_exception",
]
