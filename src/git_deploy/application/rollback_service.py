"""Latest-deployment rollback preview and confirmed execution facade."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from git_deploy.expected_state import ExpectedStateStore
from git_deploy.models import DeploymentManifest
from git_deploy.target_identity import default_state_base

from .config_service import ApplicationConfigService, ProjectSelection
from .deploy_service import ConfirmationGrant, EventSink, _require_confirmation
from .events import (
    OperationStartedEvent,
    TargetResolvedEvent,
    TerminalResultEvent,
    TransactionStage,
    TransactionStageEvent,
)
from .models import RollbackRequest, SideEffectLevel
from .plan_token import OperationPlanToken, PlanTokenSigner, StalePlanError
from .policy import confirmation_policy_for
from .results import ApplicationResult, ResultField
from .verify_service import _selected_manifest


@dataclass(frozen=True, slots=True)
class RollbackPathPlan:
    """One path restoration/removal in a latest rollback preview."""

    action: str
    path: str
    expected_current_sha256: str | None
    target_sha256: str | None


@dataclass(frozen=True, slots=True)
class LatestRollbackPlan:
    """Immutable latest rollback preview with signed execution token."""

    selection: ProjectSelection
    generation: int | None
    deployment_id: str
    paths: tuple[RollbackPathPlan, ...]
    plan_digest: str
    plan_token: OperationPlanToken


TransactionEmitter = Callable[[str, TransactionStage], None]
RollbackExecutor = Callable[
    [RollbackRequest, LatestRollbackPlan, TransactionEmitter],
    ApplicationResult,
]


class LatestRollbackService:
    """Preview and execute only the latest successful deployment rollback."""

    def __init__(
        self,
        config: ApplicationConfigService,
        signer: PlanTokenSigner,
        executor: RollbackExecutor,
    ):
        """Bind selection, plan signer, and domain rollback executor.

        Args:
            config: Shared application configuration service.
            signer: Signer shared between preview and execution.
            executor: Domain rollback callback owning transaction mutation.
        """

        if not isinstance(config, ApplicationConfigService):
            raise TypeError("config must be an ApplicationConfigService")
        if not isinstance(signer, PlanTokenSigner):
            raise TypeError("signer must be a PlanTokenSigner")
        if not callable(executor):
            raise TypeError("executor must be callable")
        self._config = config
        self._signer = signer
        self._executor = executor
        self._completed: dict[str, ApplicationResult] = {}
        self._lock = threading.Lock()

    def preview(self, request: RollbackRequest) -> LatestRollbackPlan:
        """Create a local-only preview for an eventual latest rollback.

        Args:
            request: Eventual remote-mutation latest rollback request.

        Returns:
            Signed immutable rollback plan.
        """

        _require_latest_mutation(request)
        _alias, _server, project, identity = self._config._resolve_domain_project(
            request.remote,
            request.project,
        )
        selection = self._config.resolve_project(request.remote, request.project)
        if (
            request.expected_target_id != selection.target_id
            or request.expected_physical_fingerprint
            != selection.physical_fingerprint
        ):
            raise StalePlanError("configured physical target changed before rollback preview")
        target_root = identity.state_root(
            default_state_base(project.name, project.local_state_dir)
        )
        current = ExpectedStateStore(target_root, identity).read_current()
        generation = current.generation if current is not None else None
        if generation != request.expected_generation:
            raise StalePlanError(
                "current generation changed before rollback preview: "
                f"expected {request.expected_generation}, actual {generation}"
            )
        manifest = _selected_manifest(project, target_root, None, True)
        paths = tuple(
            RollbackPathPlan(
                action="restore" if item.before_exists else "delete",
                path=item.path,
                expected_current_sha256=item.after_sha256,
                target_sha256=item.before_sha256,
            )
            for item in manifest.snapshots
        )
        digest = _rollback_digest(selection, generation, manifest, paths)
        token = self._signer.issue(
            request,
            policy_fingerprint=selection.policy_fingerprint,
            plan_digest=digest,
        )
        return LatestRollbackPlan(
            selection=selection,
            generation=generation,
            deployment_id=manifest.deployment_id,
            paths=paths,
            plan_digest=digest,
            plan_token=token,
        )

    def execute(
        self,
        request: RollbackRequest,
        plan: LatestRollbackPlan,
        *,
        token: OperationPlanToken,
        confirmation: ConfirmationGrant,
        event_sink: EventSink | None = None,
    ) -> ApplicationResult:
        """Execute one exact latest rollback plan idempotently.

        Args:
            request: Remote-mutation latest rollback request.
            plan: Previously reviewed rollback preview.
            token: Signed token returned with that preview.
            confirmation: Required confirmation response.
            event_sink: Optional ordered operation event consumer.

        Returns:
            Structured rollback result with derived generation.
        """

        _require_latest_mutation(request)
        if not isinstance(plan, LatestRollbackPlan):
            raise TypeError("plan must be a LatestRollbackPlan")
        if token.value != plan.plan_token.value:
            raise StalePlanError("presented token is not the reviewed rollback token")
        self._signer.verify(
            token,
            request,
            policy_fingerprint=plan.selection.policy_fingerprint,
            plan_digest=plan.plan_digest,
        )
        if (
            request.remote != plan.selection.remote_alias
            or request.project != plan.selection.project
            or request.expected_target_id != plan.selection.target_id
            or request.expected_generation != plan.generation
        ):
            raise StalePlanError("reviewed rollback plan does not match request context")
        policy = confirmation_policy_for(
            request,
            environment_risk=plan.selection.environment_risk,
        )
        _require_confirmation(policy, confirmation)

        with self._lock:
            cached = self._completed.get(token.value)
            if cached is not None:
                return cached
            sequence = 0

            def emit(event) -> None:
                """Forward an event when a sink is configured."""

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
            transaction_ids: list[str] = []

            def emit_transaction(transaction_id: str, stage: TransactionStage) -> None:
                """Convert executor transaction stages to application events."""

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

            result = self._executor(request, plan, emit_transaction)
            _validate_rollback_result(request, plan, result, transaction_ids)
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


def _require_latest_mutation(request: RollbackRequest) -> None:
    """Require the v0.3 common latest-rollback execution contract."""

    if not isinstance(request, RollbackRequest):
        raise TypeError("request must be a RollbackRequest")
    if (
        not request.latest
        or request.dry_run
        or request.side_effect is not SideEffectLevel.REMOTE_MUTATION
    ):
        raise ValueError("latest rollback service requires a latest remote-mutation request")


def _rollback_digest(
    selection: ProjectSelection,
    generation: int | None,
    manifest: DeploymentManifest,
    paths: tuple[RollbackPathPlan, ...],
) -> str:
    """Hash the exact latest rollback preview reviewed by an operator."""

    payload = {
        "target_id": selection.target_id,
        "physical_fingerprint": selection.physical_fingerprint,
        "policy_fingerprint": selection.policy_fingerprint,
        "generation": generation,
        "deployment_id": manifest.deployment_id,
        "manifest_status": manifest.status,
        "paths": [
            {
                "action": item.action,
                "path": item.path,
                "expected_current_sha256": item.expected_current_sha256,
                "target_sha256": item.target_sha256,
            }
            for item in paths
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_rollback_result(
    request: RollbackRequest,
    plan: LatestRollbackPlan,
    result: ApplicationResult,
    transaction_ids: list[str],
) -> None:
    """Require result context, transaction, and derived generation consistency."""

    if not isinstance(result, ApplicationResult):
        raise TypeError("rollback executor must return an ApplicationResult")
    if (
        result.operation is not request.operation
        or result.remote != request.remote
        or result.project != request.project
    ):
        raise ValueError("rollback result does not match request context")
    transaction = _result_field(result.fields, "transaction_id")
    if transaction_ids and transaction != transaction_ids[-1]:
        raise ValueError("rollback result transaction_id does not match emitted transaction")
    derived_generation = _result_field(result.fields, "generation")
    if plan.generation is not None and derived_generation != plan.generation + 1:
        raise ValueError("rollback result generation is not the expected derived generation")
    # Exact-deployment contract: result must report the reviewed deployment, not a
    # concurrent newer latest that the executor must not have rolled back.
    result_deployment = _result_field(result.fields, "deployment_id")
    if result_deployment is not None and result_deployment != plan.deployment_id:
        raise ValueError(
            "rollback result deployment_id does not match the reviewed plan "
            f"(result={result_deployment!r}, plan={plan.deployment_id!r})"
        )


def _result_field(fields: tuple[ResultField, ...], name: str) -> object:
    """Return one named structured result field or None."""

    for item in fields:
        if item.name == name:
            return item.value
    return None
