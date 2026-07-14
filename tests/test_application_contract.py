"""Contract tests for UI-neutral application requests, results, and errors."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from git_deploy.application import (
    ApplicationError,
    ApplicationResult,
    DeployRequest,
    ErrorCategory,
    GCRequest,
    HistoryRequest,
    OperationKind,
    OperationProgressEvent,
    OperationStartedEvent,
    OperationWarningEvent,
    PlanTokenSigner,
    PlanRequest,
    ResultField,
    ResultStatus,
    RollbackRequest,
    SideEffectLevel,
    StalePlanError,
    StateAction,
    StateRequest,
    TargetResolvedEvent,
    TerminalResultEvent,
    TransactionStageEvent,
    VerifyRequest,
    application_error_from_exception,
    confirmation_policy_fingerprint,
)
from git_deploy.errors import ConfigurationError, PolicyError, RemoteDriftError


def _target_context(side_effect: SideEffectLevel) -> dict[str, object]:
    """Return explicit target expectations shared by request fixtures."""

    return {
        "remote": "prod",
        "project": "application",
        "side_effect": side_effect,
        "expected_target_id": "tgt-example",
        "expected_physical_fingerprint": "a" * 64,
        "expected_generation": 7,
    }


def test_v03_public_contract_field_signatures_are_stable() -> None:
    """Detect accidental removal or renaming of CLI-facing contract fields."""

    expected = {
        PlanRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation", "revisions",
            "check_remote", "force",
        ),
        DeployRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation", "revisions",
            "dry_run", "check_remote", "force",
        ),
        HistoryRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation", "limit",
            "offset", "deployment_id",
        ),
        VerifyRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation",
            "deployment_id", "latest", "remote_check",
        ),
        RollbackRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation",
            "deployment_id", "latest", "dry_run", "check_remote", "force",
        ),
        StateRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation", "action",
            "execute", "check_remote", "revision", "empty",
        ),
        GCRequest: (
            "remote", "project", "side_effect", "expected_target_id",
            "expected_physical_fingerprint", "expected_generation", "execute",
            "plan_token",
        ),
        ApplicationResult: (
            "operation", "remote", "project", "side_effect", "status",
            "summary", "fields", "warnings",
        ),
        OperationStartedEvent: (
            "operation", "remote", "project", "sequence", "side_effect",
        ),
        TargetResolvedEvent: (
            "operation", "remote", "project", "sequence", "target_id",
            "physical_fingerprint", "generation",
        ),
        OperationWarningEvent: (
            "operation", "remote", "project", "sequence", "code", "message",
        ),
        OperationProgressEvent: (
            "operation", "remote", "project", "sequence", "progress",
        ),
        TransactionStageEvent: (
            "operation", "remote", "project", "sequence", "transaction_id",
            "stage",
        ),
        TerminalResultEvent: (
            "operation", "remote", "project", "sequence", "result",
        ),
    }

    assert {
        contract.__name__: tuple(item.name for item in fields(contract))
        for contract in expected
    } == {
        contract.__name__: signature for contract, signature in expected.items()
    }


def test_v03_operation_and_side_effect_enum_values_are_stable() -> None:
    """Prevent adapters from silently reinterpreting operation side effects."""

    assert tuple(item.value for item in SideEffectLevel) == (
        "local_read",
        "remote_read",
        "local_mutation",
        "remote_mutation",
    )
    assert tuple(item.value for item in OperationKind) == (
        "plan",
        "deploy",
        "history",
        "verify",
        "rollback",
        "state",
        "gc",
    )


def test_request_contract_covers_every_operation_family() -> None:
    """Expose typed immutable requests for plan/deploy/history/verify/rollback/state/GC."""

    requests = (
        PlanRequest(
            **_target_context(SideEffectLevel.LOCAL_READ),
            revisions=("HEAD~1..HEAD",),
        ),
        DeployRequest(
            **_target_context(SideEffectLevel.REMOTE_MUTATION),
            revisions=("HEAD",),
        ),
        HistoryRequest(**_target_context(SideEffectLevel.LOCAL_READ)),
        VerifyRequest(
            **_target_context(SideEffectLevel.REMOTE_READ),
            latest=True,
        ),
        RollbackRequest(
            **_target_context(SideEffectLevel.REMOTE_MUTATION),
            latest=True,
        ),
        StateRequest(
            **_target_context(SideEffectLevel.LOCAL_READ),
            action=StateAction.INSPECT,
        ),
        GCRequest(
            **_target_context(SideEffectLevel.LOCAL_READ),
        ),
    )

    assert tuple(request.operation for request in requests) == tuple(OperationKind)
    for request in requests:
        assert request.remote == "prod"
        assert request.project == "application"
        assert request.expected_target_id == "tgt-example"
        assert request.expected_physical_fingerprint == "a" * 64
        assert request.expected_generation == 7
        with pytest.raises(FrozenInstanceError):
            request.project = "other"  # type: ignore[misc]


def test_request_contract_freezes_sequences_and_validates_side_effects() -> None:
    """Prevent mutable selector aliasing and side-effect flag mismatches."""

    selectors = ["HEAD~1..HEAD"]
    request = PlanRequest(
        **_target_context(SideEffectLevel.LOCAL_READ),
        revisions=selectors,  # type: ignore[arg-type]
    )
    selectors.append("HEAD")
    assert request.revisions == ("HEAD~1..HEAD",)
    assert PlanRequest(
        **_target_context(SideEffectLevel.LOCAL_READ),
        revisions=(),
    ).revisions == ()

    with pytest.raises(ValueError, match="requires side_effect=remote_read"):
        PlanRequest(
            **_target_context(SideEffectLevel.LOCAL_READ),
            revisions=("HEAD",),
            check_remote=True,
        )
    with pytest.raises(ValueError, match="check_remote requires dry_run"):
        DeployRequest(
            **_target_context(SideEffectLevel.REMOTE_MUTATION),
            revisions=("HEAD",),
            check_remote=True,
        )
    with pytest.raises(ValueError, match="exactly one"):
        VerifyRequest(
            **_target_context(SideEffectLevel.REMOTE_READ),
        )


def test_request_contract_allows_explicit_prebootstrap_generation() -> None:
    """Represent a target with known identity but no current generation."""

    context = _target_context(SideEffectLevel.LOCAL_MUTATION)
    context["expected_generation"] = None
    request = StateRequest(
        **context,
        action=StateAction.BOOTSTRAP,
        execute=True,
        empty=True,
    )

    assert request.expected_generation is None
    assert request.side_effect is SideEffectLevel.LOCAL_MUTATION


def test_result_contract_is_immutable_and_renderer_neutral() -> None:
    """Allow structured scalar data while rejecting arbitrary renderer objects."""

    result = ApplicationResult(
        operation=OperationKind.PLAN,
        remote="dev",
        project="application",
        side_effect=SideEffectLevel.LOCAL_READ,
        status=ResultStatus.PLANNED,
        summary="2 changes",
        fields=[
            ResultField("generation", 7),
            ResultField("paths", ["a.txt", "b.txt"]),  # type: ignore[arg-type]
        ],  # type: ignore[arg-type]
        warnings=["host runner has user permissions"],  # type: ignore[arg-type]
    )

    assert result.fields[1].value == ("a.txt", "b.txt")
    assert result.warnings == ("host runner has user permissions",)
    with pytest.raises(FrozenInstanceError):
        result.status = ResultStatus.SUCCEEDED  # type: ignore[misc]

    class FakeRenderer:
        """Stand in for a CLI/TUI renderer that must never enter results."""

    with pytest.raises(TypeError, match="renderer/widget"):
        ResultField("renderer", FakeRenderer())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ResultField"):
        ApplicationResult(
            operation=OperationKind.PLAN,
            remote="dev",
            project="application",
            side_effect=SideEffectLevel.LOCAL_READ,
            status=ResultStatus.PLANNED,
            summary="bad",
            fields=(FakeRenderer(),),  # type: ignore[arg-type]
        )


def test_error_contract_has_stable_code_category_and_redacted_context() -> None:
    """Recursively mask secret context and never retain arbitrary object reprs."""

    sentinel = "DO-NOT-LEAK-SECRET"

    class FakeRenderer:
        """Renderer whose repr contains a sentinel to detect accidental retention."""

        def __repr__(self) -> str:
            return f"<renderer {sentinel}>"

    error = ApplicationError(
        code="build.secret-failed",
        category=ErrorCategory.EXECUTION,
        message="failed to resolve op://vault/item/field with Bearer abc123",
        context={
            "token": sentinel,
            "reference_uri": "op://vault/item/field",
            "nested": {
                "authorization": f"Bearer {sentinel}",
                "safe_stage": "build",
            },
            "renderer": FakeRenderer(),
            "argv": ["tool", f"token={sentinel}"],
        },
    )
    serialized = repr(error.to_dict())

    assert error.code == "build.secret-failed"
    assert error.category is ErrorCategory.EXECUTION
    assert "op://" not in error.message
    assert "abc123" not in error.message
    assert sentinel not in serialized
    assert "<FakeRenderer>" in serialized
    assert "safe_stage" in serialized
    assert "'build'" in serialized


@pytest.mark.parametrize(
    ("source", "code", "category"),
    [
        (
            ConfigurationError("bad config"),
            "configuration.invalid",
            ErrorCategory.CONFIGURATION,
        ),
        (PolicyError("blocked"), "policy.blocked", ErrorCategory.POLICY),
        (RemoteDriftError("drift"), "remote.drift", ErrorCategory.REMOTE),
        (KeyboardInterrupt(), "operation.cancelled", ErrorCategory.CANCELLED),
        (RuntimeError("boom"), "internal.unexpected", ErrorCategory.INTERNAL),
    ],
)
def test_error_adapter_maps_domain_failures_stably(
    source: BaseException,
    code: str,
    category: ErrorCategory,
) -> None:
    """Map domain exceptions to adapter-independent codes and categories."""

    error = application_error_from_exception(
        source,
        context={"OP_SERVICE_ACCOUNT_TOKEN": "AUTH-SENTINEL"},
    )

    assert error.code == code
    assert error.category is category
    assert "AUTH-SENTINEL" not in repr(error.to_dict())


def test_error_contract_rejects_unstable_codes() -> None:
    """Prevent free-form error identifiers from becoming adapter contracts."""

    with pytest.raises(ValueError, match="stable lowercase"):
        ApplicationError(
            code="Bad Error",
            category=ErrorCategory.INTERNAL,
            message="bad",
        )


def test_stale_plan_token_binds_request_target_policy_generation_and_plan() -> None:
    """Accept only the exact request and execution boundaries used at planning."""

    signer = PlanTokenSigner(b"p" * 32)
    request = DeployRequest(
        **_target_context(SideEffectLevel.REMOTE_MUTATION),
        revisions=("HEAD",),
    )
    token = signer.issue(
        request,
        policy_fingerprint="policy-v1",
        plan_digest="plan-v1",
    )

    signer.verify(
        token,
        request,
        policy_fingerprint="policy-v1",
        plan_digest="plan-v1",
    )
    assert str(token).startswith("v1.")


@pytest.mark.parametrize(
    ("changed_request", "policy_fingerprint", "plan_digest"),
    [
        (
            DeployRequest(
                **_target_context(SideEffectLevel.REMOTE_MUTATION),
                revisions=("HEAD~1",),
            ),
            "policy-v1",
            "plan-v1",
        ),
        (
            DeployRequest(
                **{
                    **_target_context(SideEffectLevel.REMOTE_MUTATION),
                    "expected_target_id": "tgt-other",
                },
                revisions=("HEAD",),
            ),
            "policy-v1",
            "plan-v1",
        ),
        (
            DeployRequest(
                **{
                    **_target_context(SideEffectLevel.REMOTE_MUTATION),
                    "expected_generation": 8,
                },
                revisions=("HEAD",),
            ),
            "policy-v1",
            "plan-v1",
        ),
    ],
)
def test_stale_plan_token_rejects_changed_request_boundaries(
    changed_request: DeployRequest,
    policy_fingerprint: str,
    plan_digest: str,
) -> None:
    """Reject changed selectors, target identity, or generation."""

    signer = PlanTokenSigner(b"p" * 32)
    original = DeployRequest(
        **_target_context(SideEffectLevel.REMOTE_MUTATION),
        revisions=("HEAD",),
    )
    token = signer.issue(
        original,
        policy_fingerprint="policy-v1",
        plan_digest="plan-v1",
    )

    with pytest.raises(StalePlanError, match="stale"):
        signer.verify(
            token,
            changed_request,
            policy_fingerprint=policy_fingerprint,
            plan_digest=plan_digest,
        )


@pytest.mark.parametrize(
    ("policy_fingerprint", "plan_digest"),
    [("policy-v2", "plan-v1"), ("policy-v1", "plan-v2")],
)
def test_stale_plan_token_rejects_changed_policy_or_plan_digest(
    policy_fingerprint: str,
    plan_digest: str,
) -> None:
    """Reject current policy or structured plan changes after review."""

    signer = PlanTokenSigner(b"p" * 32)
    request = DeployRequest(
        **_target_context(SideEffectLevel.REMOTE_MUTATION),
        revisions=("HEAD",),
    )
    token = signer.issue(
        request,
        policy_fingerprint="policy-v1",
        plan_digest="plan-v1",
    )

    with pytest.raises(StalePlanError, match="stale"):
        signer.verify(
            token,
            request,
            policy_fingerprint=policy_fingerprint,
            plan_digest=plan_digest,
        )


def test_stale_plan_policy_fingerprint_is_canonical() -> None:
    """Produce stable policy fingerprints without renderer-specific data."""

    from git_deploy.application import ConfirmationPolicy, ConfirmationRequirement, RiskLevel

    policy = ConfirmationPolicy(
        requirement=ConfirmationRequirement.CONFIRM,
        level=RiskLevel.NORMAL,
        risks=(),
    )

    assert confirmation_policy_fingerprint(policy) == confirmation_policy_fingerprint(policy)
