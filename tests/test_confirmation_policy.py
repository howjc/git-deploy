"""Application confirmation policy contract tests."""

from __future__ import annotations

import pytest

from git_deploy.application import (
    ConfirmationRequirement,
    DeployRequest,
    EnvironmentRisk,
    GCRequest,
    HistoryRequest,
    RiskFactor,
    RiskLevel,
    RollbackRequest,
    SideEffectLevel,
    StateAction,
    StateRequest,
    confirmation_policy_for,
)


def _target_context(side_effect: SideEffectLevel, *, remote: str = "dev") -> dict[str, object]:
    """Return explicit target expectations shared by policy fixtures."""

    return {
        "remote": remote,
        "project": "application",
        "side_effect": side_effect,
        "expected_target_id": "tgt-example",
        "expected_physical_fingerprint": "a" * 64,
        "expected_generation": 7,
    }


def test_read_only_and_ordinary_mutation_confirmation_levels() -> None:
    """Require explicit confirmation only for an ordinary mutation."""

    read_policy = confirmation_policy_for(
        HistoryRequest(**_target_context(SideEffectLevel.LOCAL_READ)),
        environment_risk=EnvironmentRisk.STANDARD,
    )
    mutation_policy = confirmation_policy_for(
        DeployRequest(
            **_target_context(SideEffectLevel.REMOTE_MUTATION),
            revisions=("HEAD",),
        ),
        environment_risk=EnvironmentRisk.STANDARD,
    )

    assert read_policy.requirement is ConfirmationRequirement.NONE
    assert read_policy.level is RiskLevel.NONE
    assert mutation_policy.requirement is ConfirmationRequirement.CONFIRM
    assert mutation_policy.level is RiskLevel.NORMAL
    assert tuple(item.factor for item in mutation_policy.risks) == (
        RiskFactor.MUTATION,
    )


def test_production_risk_is_explicit_without_requiring_a_phrase() -> None:
    """Keep production visible while allowing routine automation via ``--yes``."""

    misleading_alias = DeployRequest(
        **_target_context(SideEffectLevel.REMOTE_MUTATION, remote="prod"),
        revisions=("HEAD",),
    )
    explicit_production = DeployRequest(
        **_target_context(SideEffectLevel.REMOTE_MUTATION, remote="sandbox"),
        revisions=("HEAD",),
    )

    standard = confirmation_policy_for(
        misleading_alias,
        environment_risk=EnvironmentRisk.STANDARD,
    )
    production = confirmation_policy_for(
        explicit_production,
        environment_risk=EnvironmentRisk.PRODUCTION,
    )

    assert standard.requirement is ConfirmationRequirement.CONFIRM
    assert RiskFactor.PRODUCTION not in {item.factor for item in standard.risks}
    assert production.requirement is ConfirmationRequirement.CONFIRM
    assert RiskFactor.PRODUCTION in {item.factor for item in production.risks}
    assert production.phrase is None


def test_force_and_secret_mutations_remain_visible_without_phrase_entry() -> None:
    """Report force and secret risk while keeping routine deploys script-friendly."""

    request = DeployRequest(
        **_target_context(SideEffectLevel.REMOTE_MUTATION),
        revisions=("HEAD",),
        force=True,
    )
    policy = confirmation_policy_for(
        request,
        environment_risk=EnvironmentRisk.STANDARD,
        uses_secret=True,
    )

    assert policy.requirement is ConfirmationRequirement.CONFIRM
    assert policy.level is RiskLevel.HIGH
    assert {item.factor for item in policy.risks} >= {
        RiskFactor.FORCE,
        RiskFactor.SECRET,
    }
    assert policy.phrase is None


@pytest.mark.parametrize(
    ("operation_request", "factor"),
    [
        (
            RollbackRequest(
                **_target_context(SideEffectLevel.REMOTE_MUTATION),
                deployment_id="old-deployment",
            ),
            RiskFactor.HISTORICAL_ROLLBACK,
        ),
        (
            GCRequest(
                **_target_context(SideEffectLevel.LOCAL_MUTATION),
                execute=True,
                plan_token="gc-plan",
            ),
            RiskFactor.GC_DELETE,
        ),
        (
            StateRequest(
                **_target_context(SideEffectLevel.REMOTE_MUTATION),
                action=StateAction.RECOVER,
                execute=True,
            ),
            RiskFactor.RECOVERY,
        ),
    ],
)
def test_critical_operations_require_target_bound_phrase(
    operation_request: object,
    factor: RiskFactor,
) -> None:
    """Classify historical rollback, GC deletion, and recovery as critical."""

    policy = confirmation_policy_for(
        operation_request,  # type: ignore[arg-type]
        environment_risk=EnvironmentRisk.STANDARD,
    )

    assert policy.requirement is ConfirmationRequirement.PHRASE
    assert policy.level is RiskLevel.CRITICAL
    assert factor in {item.factor for item in policy.risks}
    assert policy.phrase is not None and policy.phrase.endswith("tgt-example")
