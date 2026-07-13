"""Shared application configuration selection service tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.application import (
    ApplicationConfigService,
    EnvironmentRisk,
    SelectionState,
)
from git_deploy.errors import ConfigurationError


def _write_config(tmp_path: Path, *, prod_risk: str = "production") -> Path:
    """Write a two-remote fixture with explicit roots and risk classification."""

    repository = tmp_path / "repository"
    repository.mkdir()
    path = tmp_path / "deploy.toml"
    path.write_text(
        f"""
default_remote = "dev"

[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"
risk = "standard"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"
risk = "{prod_risk}"

[projects.demo]
repository = "{repository}"

[projects.demo.remotes.dev]
remote_root = "/srv/dev/demo"

[projects.demo.remotes.prod]
remote_root = "/srv/prod/demo"
""".strip(),
        encoding="utf-8",
    )
    return path


def test_application_config_resolves_alias_physical_target_and_risk_summary(
    tmp_path: Path,
) -> None:
    """Return renderer-safe target identity and explicit environment risk."""

    service = ApplicationConfigService.from_path(_write_config(tmp_path))

    dev = service.resolve_project("dev", "demo")
    prod = service.resolve_project("prod", "demo")

    assert dev.remote_alias == "dev"
    assert dev.endpoint == "sftp://dev.example.invalid:22"
    assert dev.remote_root == "/srv/dev/demo"
    assert dev.target_id.startswith("tgt-")
    assert len(dev.physical_fingerprint) == 64
    assert len(dev.policy_fingerprint) == 64
    assert dev.environment_risk is EnvironmentRisk.STANDARD
    assert prod.environment_risk is EnvironmentRisk.PRODUCTION
    assert prod.target_id != dev.target_id


def test_application_config_never_guesses_production_risk_from_alias(
    tmp_path: Path,
) -> None:
    """Treat an alias named prod as standard unless risk is explicitly configured."""

    service = ApplicationConfigService.from_path(
        _write_config(tmp_path, prod_risk="standard")
    )

    selection = service.resolve_project("prod", "demo")

    assert selection.remote_alias == "prod"
    assert selection.environment_risk is EnvironmentRisk.STANDARD


def test_application_config_remote_switch_clears_project_and_confirmation(
    tmp_path: Path,
) -> None:
    """Prevent a reviewed dev plan from surviving a switch to production."""

    service = ApplicationConfigService.from_path(_write_config(tmp_path))
    state = service.switch_remote(SelectionState(), "dev")
    state = service.select_project(state, "demo")
    state = service.record_confirmation(state, plan_token="signed-dev-plan")

    switched = service.switch_remote(state, "prod")

    assert switched.remote_alias == "prod"
    assert switched.project is None
    assert switched.plan_token is None
    assert switched.confirmed is False


def test_application_config_rejects_invalid_explicit_risk_and_unknown_project(
    tmp_path: Path,
) -> None:
    """Fail closed for unsupported risk labels and project keys."""

    invalid = ApplicationConfigService.from_path(
        _write_config(tmp_path, prod_risk="dangerous")
    )
    with pytest.raises(ConfigurationError, match="risk must be one of"):
        invalid.resolve_project("prod", "demo")
    with pytest.raises(ConfigurationError, match="unknown project"):
        invalid.resolve_project("dev", "missing")
