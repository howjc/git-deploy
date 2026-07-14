"""Read-only application, repository, and durable-state diagnostics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .config_service import ApplicationConfigService
from .errors import ApplicationError, ErrorCategory
from .models import SideEffectLevel


class DoctorCheckStatus(StrEnum):
    """Stable outcome values for one independent diagnostic check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class DoctorCheckCategory(StrEnum):
    """Stable execution groups used to render doctor results."""

    LOCAL = "local"
    STATE = "state"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorRequest:
    """Select one project set and the maximum diagnostic side effect."""

    remote: str | None
    target: str
    check_remote: bool = False

    def __post_init__(self) -> None:
        """Validate selectors while retaining an explicit read boundary."""

        if self.remote is not None and (
            not isinstance(self.remote, str) or not self.remote.strip()
        ):
            raise ValueError("remote must be a non-empty string or None")
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("target must be a non-empty string")

    @property
    def side_effect(self) -> SideEffectLevel:
        """Return local-read or remote-read according to the explicit flag."""

        return (
            SideEffectLevel.REMOTE_READ
            if self.check_remote
            else SideEffectLevel.LOCAL_READ
        )


@dataclass(frozen=True, slots=True)
class DoctorContextItem:
    """One immutable, recursively sanitized diagnostic context value."""

    key: str
    value: object


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorCheckResult:
    """Renderer-neutral outcome for one diagnostic check."""

    check_id: str
    category: DoctorCheckCategory
    status: DoctorCheckStatus
    summary: str
    side_effect: SideEffectLevel
    context: tuple[DoctorContextItem, ...] = ()
    suggested_action: str | None = None

    def __post_init__(self) -> None:
        """Validate stable identifiers and freeze sanitized context."""

        if not isinstance(self.check_id, str) or "." not in self.check_id:
            raise ValueError("check_id must be a stable dotted identifier")
        if not isinstance(self.category, DoctorCheckCategory):
            raise TypeError("category must be a DoctorCheckCategory")
        if not isinstance(self.status, DoctorCheckStatus):
            raise TypeError("status must be a DoctorCheckStatus")
        if not isinstance(self.side_effect, SideEffectLevel):
            raise TypeError("side_effect must be a SideEffectLevel")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("summary must be a non-empty string")
        if self.suggested_action is not None and not self.suggested_action.strip():
            raise ValueError("suggested_action must be non-empty or None")

    @classmethod
    def create(
        cls,
        *,
        check_id: str,
        category: DoctorCheckCategory,
        status: DoctorCheckStatus,
        summary: str,
        side_effect: SideEffectLevel,
        context: Mapping[str, Any] | None = None,
        suggested_action: str | None = None,
    ) -> DoctorCheckResult:
        """Create a check after reusing application-error secret redaction."""

        sanitized = ApplicationError(
            code="doctor.context",
            category=ErrorCategory.INTERNAL,
            message="doctor context",
            context=context,
        ).to_dict()["context"]
        if not isinstance(sanitized, dict):
            raise TypeError("sanitized doctor context must be a mapping")
        return cls(
            check_id=check_id,
            category=category,
            status=status,
            summary=summary,
            side_effect=side_effect,
            context=tuple(
                DoctorContextItem(str(key), value)
                for key, value in sorted(sanitized.items())
            ),
            suggested_action=suggested_action,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a serialization-ready, already-redacted representation."""

        return {
            "id": self.check_id,
            "category": self.category.value,
            "status": self.status.value,
            "summary": self.summary,
            "side_effect": self.side_effect.value,
            "context": {item.key: item.value for item in self.context},
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorResult:
    """Complete immutable doctor report with stable readiness semantics."""

    schema_version: int
    remote: str | None
    target: str
    checks: tuple[DoctorCheckResult, ...]

    def __post_init__(self) -> None:
        """Freeze checks and require the current schema version."""

        if self.schema_version != 1:
            raise ValueError("doctor schema_version must be 1")
        if any(not isinstance(item, DoctorCheckResult) for item in self.checks):
            raise TypeError("checks must contain DoctorCheckResult values")

    @property
    def ready_label(self) -> str:
        """Return the final human-readable readiness label."""

        if any(item.status is DoctorCheckStatus.FAIL for item in self.checks):
            return "NOT READY"
        if any(item.status is DoctorCheckStatus.WARN for item in self.checks):
            return "READY WITH WARNINGS"
        return "READY"

    @property
    def exit_code(self) -> int:
        """Return zero for ready reports and configuration-style four for failures."""

        return 4 if self.ready_label == "NOT READY" else 0

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON-compatible doctor report schema."""

        return {
            "schema_version": self.schema_version,
            "remote": self.remote,
            "target": self.target,
            "ready": self.ready_label,
            "checks": [item.to_dict() for item in self.checks],
        }


DoctorCheck = Callable[[DoctorRequest], DoctorCheckResult]


class DoctorService:
    """Run independent read-only checks without short-circuiting on failures."""

    def __init__(
        self,
        config: ApplicationConfigService,
        *,
        local_checks: tuple[DoctorCheck, ...] = (),
        state_checks: tuple[DoctorCheck, ...] = (),
        remote_checks: tuple[DoctorCheck, ...] = (),
    ):
        """Bind parsed configuration and check groups.

        Args:
            config: Parsed application configuration used by concrete checks.
            local_checks: Local configuration/repository checks.
            state_checks: Durable local-state checks.
            remote_checks: Explicitly enabled read-only transport checks.
        """

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        self.config = config
        self._local_checks = tuple(local_checks)
        self._state_checks = tuple(state_checks)
        self._remote_checks = tuple(remote_checks)

    def run(self, request: DoctorRequest) -> DoctorResult:
        """Run every independent check and convert exceptions into safe failures."""

        if not isinstance(request, DoctorRequest):
            raise TypeError("request must be a DoctorRequest")
        selected = self._local_checks + self._state_checks
        if request.check_remote:
            selected += self._remote_checks
        checks = tuple(self._run_one(check, request) for check in selected)
        return DoctorResult(
            schema_version=1,
            remote=request.remote,
            target=request.target,
            checks=checks,
        )

    @staticmethod
    def _run_one(check: DoctorCheck, request: DoctorRequest) -> DoctorCheckResult:
        """Run one check without preventing later independent checks."""

        try:
            result = check(request)
            if not isinstance(result, DoctorCheckResult):
                raise TypeError("doctor check returned an invalid result")
            return result
        except Exception as exc:
            error = ApplicationError(
                code="doctor.check-failed",
                category=ErrorCategory.CONFIGURATION,
                message=str(exc) or exc.__class__.__name__,
                context={"exception_type": exc.__class__.__name__},
            )
            return DoctorCheckResult.create(
                check_id="doctor.check-failed",
                category=DoctorCheckCategory.LOCAL,
                status=DoctorCheckStatus.FAIL,
                summary=error.message,
                side_effect=SideEffectLevel.LOCAL_READ,
                context={item.key: item.value for item in error.context},
                suggested_action="fix the reported configuration and rerun doctor",
            )
