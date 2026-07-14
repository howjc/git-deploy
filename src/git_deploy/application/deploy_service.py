"""Confirmed, idempotent deployment application service facade."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .events import (
    ApplicationEvent,
    OperationStartedEvent,
    OperationWarningEvent,
    TargetResolvedEvent,
    TerminalResultEvent,
    TransactionStage,
    TransactionStageEvent,
)
from .models import DeployRequest, SideEffectLevel
from .plan_service import RevisionPlanResult
from .plan_token import OperationPlanToken, PlanTokenSigner, StalePlanError
from .policy import (
    ConfirmationPolicy,
    ConfirmationRequirement,
    confirmation_policy_for,
)
from .results import ApplicationResult, ResultField, ResultStatus


@dataclass(frozen=True, slots=True)
class ConfirmationGrant:
    """Adapter-supplied confirmation response for one reviewed plan."""

    confirmed: bool
    phrase: str | None = None


EventSink = Callable[[ApplicationEvent], None]
TransactionEmitter = Callable[[str, TransactionStage], None]
DeployExecutor = Callable[
    [DeployRequest, RevisionPlanResult, TransactionEmitter],
    ApplicationResult,
]


class DeployService:
    """Validate plan/confirmation and run one deployment exactly once per token."""

    def __init__(
        self,
        signer: PlanTokenSigner,
        executor: DeployExecutor,
    ):
        """Bind plan signer and a domain executor adapter.

        Args:
            signer: Signer used when the reviewed plan token was issued.
            executor: Domain deployment callback; it owns transaction mutation.
        """

        if not isinstance(signer, PlanTokenSigner):
            raise TypeError("signer must be a PlanTokenSigner")
        if not callable(executor):
            raise TypeError("executor must be callable")
        self._signer = signer
        self._executor = executor
        self._completed: dict[str, ApplicationResult] = {}
        self._lock = threading.Lock()

    def execute(
        self,
        request: DeployRequest,
        plan: RevisionPlanResult,
        *,
        token: OperationPlanToken,
        confirmation: ConfirmationGrant,
        event_sink: EventSink | None = None,
    ) -> ApplicationResult:
        """Execute an exact reviewed deployment once.

        Args:
            request: Eventual remote-mutation deployment request.
            plan: Structured plan reviewed by the operator.
            token: Signed credential returned with that exact plan.
            confirmation: Explicit confirmation or high-risk phrase.
            event_sink: Optional ordered operation event consumer.

        Returns:
            Structured terminal result; repeated successful calls return it.
        """

        if not isinstance(request, DeployRequest):
            raise TypeError("request must be a DeployRequest")
        if request.side_effect is not SideEffectLevel.REMOTE_MUTATION or request.dry_run:
            raise ValueError("deploy execution requires a remote-mutation request")
        if not isinstance(plan, RevisionPlanResult):
            raise TypeError("plan must be a RevisionPlanResult")
        if not isinstance(confirmation, ConfirmationGrant):
            raise TypeError("confirmation must be a ConfirmationGrant")
        if token.value != plan.plan_token.value:
            raise StalePlanError("presented token is not the reviewed plan token")
        self._signer.verify(
            token,
            request,
            policy_fingerprint=plan.selection.policy_fingerprint,
            plan_digest=plan.plan_digest,
        )
        if (
            plan.selection.remote_alias != request.remote
            or plan.selection.project != request.project
            or plan.selection.target_id != request.expected_target_id
            or plan.generation != request.expected_generation
        ):
            raise StalePlanError("reviewed plan does not match deploy request context")
        static_noop = _is_static_noop(plan)
        if not static_noop:
            policy = confirmation_policy_for(
                request,
                environment_risk=plan.selection.environment_risk,
                uses_secret=plan.build.uses_secret,
            )
            _require_confirmation(policy, confirmation)

        with self._lock:
            cached = self._completed.get(token.value)
            if cached is not None:
                return cached
            sequence = 0

            def emit(event: ApplicationEvent) -> None:
                """Forward one event when a sink is configured."""

                if event_sink is not None:
                    event_sink(event)

            emit(
                OperationStartedEvent(
                    operation=request.operation,
                    remote=request.remote,
                    project=request.project,
                    sequence=sequence,
                    side_effect=request.side_effect,
                )
            )
            sequence += 1
            emit(
                TargetResolvedEvent(
                    operation=request.operation,
                    remote=request.remote,
                    project=request.project,
                    sequence=sequence,
                    target_id=plan.selection.target_id,
                    physical_fingerprint=plan.selection.physical_fingerprint,
                    generation=plan.generation,
                )
            )
            sequence += 1
            for warning in plan.warnings:
                emit(
                    OperationWarningEvent(
                        operation=request.operation,
                        remote=request.remote,
                        project=request.project,
                        sequence=sequence,
                        code="operation.warning",
                        message=warning,
                    )
                )
                sequence += 1
            transaction_ids: list[str] = []

            def emit_transaction(transaction_id: str, stage: TransactionStage) -> None:
                """Convert executor stages to ordered application events."""

                nonlocal sequence
                transaction_ids.append(transaction_id)
                emit(
                    TransactionStageEvent(
                        operation=request.operation,
                        remote=request.remote,
                        project=request.project,
                        sequence=sequence,
                        transaction_id=transaction_id,
                        stage=stage,
                    )
                )
                sequence += 1

            # Static no-op still runs the executor so domain can re-read current
            # under target lock and reject a stale plan (zero remote mutation).
            result = self._executor(request, plan, emit_transaction)
            if static_noop and result.status is ResultStatus.SUCCEEDED:
                # Domain adapters may report success for already-deployed; normalize.
                result = ApplicationResult(
                    operation=request.operation,
                    remote=request.remote,
                    project=request.project,
                    side_effect=request.side_effect,
                    status=ResultStatus.NOOP,
                    summary=result.summary
                    or (
                        "No changes: target generation already matches "
                        f"{plan.after_tree_id}"
                    ),
                    fields=result.fields
                    or (
                        ResultField("generation", plan.generation),
                        ResultField("target_tree_id", plan.after_tree_id),
                    ),
                )
            _validate_result(request, result, transaction_ids)
            emit(
                TerminalResultEvent(
                    operation=request.operation,
                    remote=request.remote,
                    project=request.project,
                    sequence=sequence,
                    result=result,
                )
            )
            self._completed[token.value] = result
            return result


def _is_static_noop(plan: RevisionPlanResult) -> bool:
    """Return whether a reviewed source-only plan requires no execution.

    Artifact builds remain actionable because their outputs cannot be proven
    unchanged from the local revision plan alone.
    """

    return bool(
        plan.static_noop
        and not plan.build.enabled
        and not plan.source_changes
        and not plan.introduced_transition_ids
    )


def _require_confirmation(
    policy: ConfirmationPolicy,
    grant: ConfirmationGrant,
) -> None:
    """Reject missing or incorrect confirmation for the derived risk policy."""

    if policy.requirement is ConfirmationRequirement.NONE:
        return
    if not grant.confirmed:
        raise PermissionError("deployment confirmation is required")
    if (
        policy.requirement is ConfirmationRequirement.PHRASE
        and grant.phrase != policy.phrase
    ):
        raise PermissionError("deployment confirmation phrase does not match")


def _validate_result(
    request: DeployRequest,
    result: ApplicationResult,
    transaction_ids: list[str],
) -> None:
    """Ensure terminal result and executor transaction events are consistent."""

    if not isinstance(result, ApplicationResult):
        raise TypeError("deploy executor must return an ApplicationResult")
    if (
        result.operation is not request.operation
        or result.remote != request.remote
        or result.project != request.project
    ):
        raise ValueError("deploy result does not match request context")
    transaction = _field(result.fields, "transaction_id")
    if transaction_ids and transaction != transaction_ids[-1]:
        raise ValueError("deploy result transaction_id does not match emitted transaction")


def _field(fields: tuple[ResultField, ...], name: str) -> object:
    """Return one named structured result field or None."""

    for item in fields:
        if item.name == name:
            return item.value
    return None
