"""Immutable application request models independent of CLI/TUI renderers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeAlias


class SideEffectLevel(StrEnum):
    """Declare the maximum observable side effect of an operation request."""

    LOCAL_READ = "local_read"
    REMOTE_READ = "remote_read"
    LOCAL_MUTATION = "local_mutation"
    REMOTE_MUTATION = "remote_mutation"


class OperationKind(StrEnum):
    """Stable operation identifiers shared by every application adapter."""

    PLAN = "plan"
    DEPLOY = "deploy"
    HISTORY = "history"
    VERIFY = "verify"
    ROLLBACK = "rollback"
    STATE = "state"
    GC = "gc"


class StateAction(StrEnum):
    """State operation variants carried by StateRequest."""

    INSPECT = "inspect"
    VERIFY = "verify"
    BOOTSTRAP = "bootstrap"
    RECOVER = "recover"
    MIGRATE = "migrate"
    POLICY_MIGRATE = "policy_migrate"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationRequest:
    """Common immutable target expectations for one application operation.

    Args:
        remote: Explicit selected remote alias.
        project: Explicit selected project key.
        side_effect: Maximum side-effect level authorized for this request.
        expected_target_id: Physical target identity expected at execution.
        expected_physical_fingerprint: Canonical endpoint/root fingerprint.
        expected_generation: Expected current generation, or None before bootstrap.
    """

    operation: ClassVar[OperationKind]
    remote: str
    project: str
    side_effect: SideEffectLevel
    expected_target_id: str
    expected_physical_fingerprint: str
    expected_generation: int | None

    def __post_init__(self) -> None:
        """Validate explicit target context without inferring it from an alias."""

        for name in (
            "remote",
            "project",
            "expected_target_id",
            "expected_physical_fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.side_effect, SideEffectLevel):
            raise TypeError("side_effect must be a SideEffectLevel")
        generation = self.expected_generation
        if generation is not None and (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
        ):
            raise ValueError("expected_generation must be a non-negative integer or None")

    def _require_side_effect(self, expected: SideEffectLevel) -> None:
        """Reject a request whose declared side effect disagrees with its flags.

        Args:
            expected: Side-effect level implied by the concrete request.
        """

        if self.side_effect is not expected:
            raise ValueError(
                f"{self.operation.value} requires side_effect={expected.value}, "
                f"got {self.side_effect.value}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanRequest(ApplicationRequest):
    """Request a local plan or an explicitly read-only remote plan check."""

    operation: ClassVar[OperationKind] = OperationKind.PLAN
    revisions: tuple[str, ...]
    check_remote: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        """Freeze revision selectors and validate read-only side-effect intent."""

        super(PlanRequest, self).__post_init__()
        object.__setattr__(self, "revisions", _selectors(self.revisions))
        self._require_side_effect(
            SideEffectLevel.REMOTE_READ
            if self.check_remote
            else SideEffectLevel.LOCAL_READ
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeployRequest(ApplicationRequest):
    """Request deployment planning or an authorized remote mutation."""

    operation: ClassVar[OperationKind] = OperationKind.DEPLOY
    revisions: tuple[str, ...]
    dry_run: bool = False
    check_remote: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        """Freeze selectors and bind flags to the declared side-effect level."""

        super(DeployRequest, self).__post_init__()
        object.__setattr__(self, "revisions", _selectors(self.revisions))
        if self.check_remote and not self.dry_run:
            raise ValueError("deploy check_remote requires dry_run")
        expected = SideEffectLevel.REMOTE_MUTATION
        if self.dry_run:
            expected = (
                SideEffectLevel.REMOTE_READ
                if self.check_remote
                else SideEffectLevel.LOCAL_READ
            )
        self._require_side_effect(expected)


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryRequest(ApplicationRequest):
    """Request local deployment history for one project."""

    operation: ClassVar[OperationKind] = OperationKind.HISTORY
    limit: int = 20
    offset: int = 0
    deployment_id: str | None = None

    def __post_init__(self) -> None:
        """Validate history pagination and its local-read contract."""

        super(HistoryRequest, self).__post_init__()
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("limit must be a positive integer")
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
        ):
            raise ValueError("offset must be a non-negative integer")
        if self.deployment_id is not None and (
            not isinstance(self.deployment_id, str)
            or not self.deployment_id.strip()
        ):
            raise ValueError("deployment_id must be a non-empty string or None")
        self._require_side_effect(SideEffectLevel.LOCAL_READ)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifyRequest(ApplicationRequest):
    """Request a read-only remote comparison with a deployment record."""

    operation: ClassVar[OperationKind] = OperationKind.VERIFY
    deployment_id: str | None = None
    latest: bool = False
    remote_check: bool = True

    def __post_init__(self) -> None:
        """Require one deployment selector and a remote-read declaration."""

        super(VerifyRequest, self).__post_init__()
        _validate_deployment_selector(self.deployment_id, self.latest)
        self._require_side_effect(
            SideEffectLevel.REMOTE_READ
            if self.remote_check
            else SideEffectLevel.LOCAL_READ
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RollbackRequest(ApplicationRequest):
    """Request a rollback preview/read-check or an authorized rollback."""

    operation: ClassVar[OperationKind] = OperationKind.ROLLBACK
    deployment_id: str | None = None
    latest: bool = False
    dry_run: bool = False
    check_remote: bool = False
    force: bool = False

    def __post_init__(self) -> None:
        """Validate selector and bind preview flags to their side effects."""

        super(RollbackRequest, self).__post_init__()
        _validate_deployment_selector(self.deployment_id, self.latest)
        if self.check_remote and not self.dry_run:
            raise ValueError("rollback check_remote requires dry_run")
        expected = SideEffectLevel.REMOTE_MUTATION
        if self.dry_run:
            expected = (
                SideEffectLevel.REMOTE_READ
                if self.check_remote
                else SideEffectLevel.LOCAL_READ
            )
        self._require_side_effect(expected)


@dataclass(frozen=True, slots=True, kw_only=True)
class StateRequest(ApplicationRequest):
    """Request one state inspect/verify/bootstrap/recover/migration action."""

    operation: ClassVar[OperationKind] = OperationKind.STATE
    action: StateAction
    execute: bool = False
    check_remote: bool = False
    revision: str | None = None
    empty: bool = False

    def __post_init__(self) -> None:
        """Validate state action and its explicitly authorized side effects."""

        super(StateRequest, self).__post_init__()
        if not isinstance(self.action, StateAction):
            raise TypeError("action must be a StateAction")
        allowed = _STATE_SIDE_EFFECTS[self.action]
        if self.side_effect not in allowed:
            choices = ", ".join(sorted(item.value for item in allowed))
            raise ValueError(
                f"state {self.action.value} allows side_effect in {{{choices}}}, "
                f"got {self.side_effect.value}"
            )
        if self.revision and self.empty:
            raise ValueError("state request cannot combine revision and empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class GCRequest(ApplicationRequest):
    """Request a GC plan or an explicitly authorized local sweep."""

    operation: ClassVar[OperationKind] = OperationKind.GC
    execute: bool = False
    plan_token: str | None = None

    def __post_init__(self) -> None:
        """Bind GC plan/execute flags to local read/mutation levels."""

        super(GCRequest, self).__post_init__()
        expected = (
            SideEffectLevel.LOCAL_MUTATION
            if self.execute
            else SideEffectLevel.LOCAL_READ
        )
        self._require_side_effect(expected)


OperationRequest: TypeAlias = (
    PlanRequest
    | DeployRequest
    | HistoryRequest
    | VerifyRequest
    | RollbackRequest
    | StateRequest
    | GCRequest
)


_STATE_SIDE_EFFECTS: dict[StateAction, frozenset[SideEffectLevel]] = {
    StateAction.INSPECT: frozenset({SideEffectLevel.LOCAL_READ}),
    StateAction.VERIFY: frozenset(
        {SideEffectLevel.LOCAL_READ, SideEffectLevel.REMOTE_READ}
    ),
    StateAction.BOOTSTRAP: frozenset(
        {SideEffectLevel.LOCAL_READ, SideEffectLevel.LOCAL_MUTATION}
    ),
    StateAction.RECOVER: frozenset(
        {SideEffectLevel.LOCAL_READ, SideEffectLevel.REMOTE_MUTATION}
    ),
    StateAction.MIGRATE: frozenset(
        {SideEffectLevel.LOCAL_READ, SideEffectLevel.LOCAL_MUTATION}
    ),
    StateAction.POLICY_MIGRATE: frozenset(
        {SideEffectLevel.LOCAL_READ, SideEffectLevel.LOCAL_MUTATION}
    ),
}


def _selectors(values: tuple[str, ...]) -> tuple[str, ...]:
    """Return validated immutable explicit or implicit revision selectors.

    Args:
        values: Candidate selector sequence.

    Returns:
        Tuple of stripped selectors; empty means implicit current-to-HEAD selection.
    """

    if isinstance(values, str):
        raise TypeError("revisions must be a sequence, not a string")
    selectors = tuple(str(value).strip() for value in values)
    if any(not value for value in selectors):
        raise ValueError("revisions must not contain empty selectors")
    return selectors


def _validate_deployment_selector(
    deployment_id: str | None,
    latest: bool,
) -> None:
    """Require exactly one deployment selector.

    Args:
        deployment_id: Exact deployment ID or unique prefix.
        latest: Whether to select the latest successful deployment.
    """

    has_id = isinstance(deployment_id, str) and bool(deployment_id.strip())
    if has_id == latest:
        raise ValueError("select exactly one of deployment_id or latest")
