"""Explicit confirmation policy shared by CLI and future TUI adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from .models import (
    GCRequest,
    OperationRequest,
    RollbackRequest,
    SideEffectLevel,
    StateAction,
    StateRequest,
)


class EnvironmentRisk(StrEnum):
    """Explicit environment risk configured independently of remote aliases."""

    STANDARD = "standard"
    PRODUCTION = "production"


class RiskLevel(IntEnum):
    """Ordered operator-facing risk levels for one operation plan."""

    NONE = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class RiskFactor(StrEnum):
    """Stable reasons that contribute to an operation's confirmation policy."""

    MUTATION = "mutation"
    PRODUCTION = "production"
    FORCE = "force"
    SECRET = "secret"
    HISTORICAL_ROLLBACK = "historical_rollback"
    GC_DELETE = "gc_delete"
    RECOVERY = "recovery"


class ConfirmationRequirement(StrEnum):
    """Adapter-neutral confirmation interaction required before execution."""

    NONE = "none"
    CONFIRM = "confirm"
    PHRASE = "phrase"


@dataclass(frozen=True, slots=True)
class RiskItem:
    """One stable confirmation risk factor and its severity."""

    factor: RiskFactor
    level: RiskLevel


@dataclass(frozen=True, slots=True)
class ConfirmationPolicy:
    """Immutable confirmation decision derived from explicit request context.

    Args:
        requirement: Interaction required before a mutation may execute.
        level: Highest risk level among all factors.
        risks: Ordered risk factors displayed by adapters.
        phrase: Exact phrase required for high-risk mutations, otherwise None.
    """

    requirement: ConfirmationRequirement
    level: RiskLevel
    risks: tuple[RiskItem, ...]
    phrase: str | None = None

    def __post_init__(self) -> None:
        """Validate requirement and phrase consistency."""

        risks = tuple(self.risks)
        if any(not isinstance(item, RiskItem) for item in risks):
            raise TypeError("risks must contain only RiskItem values")
        object.__setattr__(self, "risks", risks)
        if self.requirement is ConfirmationRequirement.PHRASE:
            if not isinstance(self.phrase, str) or not self.phrase.strip():
                raise ValueError("phrase confirmation requires a non-empty phrase")
        elif self.phrase is not None:
            raise ValueError("phrase must be None unless phrase confirmation is required")


def confirmation_policy_for(
    request: OperationRequest,
    *,
    environment_risk: EnvironmentRisk,
    uses_secret: bool = False,
) -> ConfirmationPolicy:
    """Derive confirmation requirements from explicit operation facts.

    Args:
        request: Immutable application operation request.
        environment_risk: Explicit configured environment classification.
        uses_secret: Whether execution will inject a configured secret.

    Returns:
        Immutable confirmation policy suitable for CLI or TUI adapters.
    """

    if not isinstance(environment_risk, EnvironmentRisk):
        raise TypeError("environment_risk must be an EnvironmentRisk")
    if not isinstance(uses_secret, bool):
        raise TypeError("uses_secret must be a bool")

    risks: list[RiskItem] = []
    is_mutation = request.side_effect in {
        SideEffectLevel.LOCAL_MUTATION,
        SideEffectLevel.REMOTE_MUTATION,
    }
    if is_mutation:
        risks.append(RiskItem(RiskFactor.MUTATION, RiskLevel.NORMAL))
    if environment_risk is EnvironmentRisk.PRODUCTION:
        risks.append(RiskItem(RiskFactor.PRODUCTION, RiskLevel.HIGH))
    if bool(getattr(request, "force", False)):
        risks.append(RiskItem(RiskFactor.FORCE, RiskLevel.HIGH))
    if uses_secret:
        risks.append(RiskItem(RiskFactor.SECRET, RiskLevel.HIGH))
    if isinstance(request, RollbackRequest) and not request.latest:
        risks.append(RiskItem(RiskFactor.HISTORICAL_ROLLBACK, RiskLevel.CRITICAL))
    if isinstance(request, GCRequest) and request.execute:
        risks.append(RiskItem(RiskFactor.GC_DELETE, RiskLevel.CRITICAL))
    if (
        isinstance(request, StateRequest)
        and request.action is StateAction.RECOVER
        and request.execute
    ):
        risks.append(RiskItem(RiskFactor.RECOVERY, RiskLevel.CRITICAL))

    level = max((item.level for item in risks), default=RiskLevel.NONE)
    if not is_mutation:
        requirement = ConfirmationRequirement.NONE
        phrase = None
    elif level >= RiskLevel.CRITICAL:
        # Phrase entry is reserved for destructive/recovery operations. Routine
        # deploys and latest rollbacks remain script-friendly: ``--yes`` can
        # acknowledge production, force, and secret-related warnings.
        requirement = ConfirmationRequirement.PHRASE
        phrase = f"CONFIRM {request.operation.value.upper()} {request.expected_target_id}"
    else:
        requirement = ConfirmationRequirement.CONFIRM
        phrase = None
    return ConfirmationPolicy(
        requirement=requirement,
        level=level,
        risks=tuple(risks),
        phrase=phrase,
    )
