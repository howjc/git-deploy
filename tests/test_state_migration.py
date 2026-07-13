"""Legacy/named-remote migration plan/staging/publish tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from git_deploy.errors import PolicyError
from git_deploy.state_migration import StateMigrationService
from git_deploy.target_identity import resolve_target_identity


def _write_manifest(path: Path, deployment_id: str, body: str = "x") -> None:
    """Write a tiny legacy deployment record.

    Args:
        path: Deployments root.
        deployment_id: Id.
        body: Manifest body marker.

    Returns:
        None.
    """

    d = path / deployment_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"deployment_id": deployment_id, "body": body}), encoding="utf-8")


def test_plan_groups_by_physical_target(tmp_path: Path) -> None:
    """Discover default and alias histories grouped by physical target_id."""

    base = tmp_path / "state"
    _write_manifest(base / "deployments", "d1")
    _write_manifest(base / "remotes" / "dev" / "deployments", "d2")
    _write_manifest(base / "remotes" / "prod" / "deployments", "d3")
    identities = {
        "default": resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv"),
        "dev": resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv"),
        "prod": resolve_target_identity({"protocol": "sftp", "host": "h2"}, "demo", remote_root="/srv"),
    }
    plan = StateMigrationService(base).plan(identities)
    assert not plan.blocked
    assert "default" in plan.shared_targets[identities["default"].target_id]
    assert "dev" in plan.shared_targets[identities["dev"].target_id]
    assert identities["default"].target_id == identities["dev"].target_id
    assert identities["prod"].target_id != identities["dev"].target_id


def test_plan_blocks_conflicting_shared_history(tmp_path: Path) -> None:
    """Same deployment id with different content across aliases blocks with zero writes."""

    base = tmp_path / "state"
    _write_manifest(base / "remotes" / "dev" / "deployments", "same", body="a")
    _write_manifest(base / "remotes" / "prod" / "deployments", "same", body="b")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv")
    identities = {"dev": identity, "prod": identity}
    plan = StateMigrationService(base).plan(identities)
    assert plan.blocked


def test_staging_and_publish(tmp_path: Path) -> None:
    """Staging copies to isolation; publish durably exposes migration without deleting legacy."""

    base = tmp_path / "state"
    _write_manifest(base / "remotes" / "dev" / "deployments", "d1")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv")
    identities = {"dev": identity}
    svc = StateMigrationService(base)
    plan = svc.plan(identities)
    staging = tmp_path / "staging"
    svc.stage(plan, staging)
    assert (staging / "staging.json").is_file()
    assert (base / "remotes" / "dev" / "deployments" / "d1").is_dir()  # legacy remains
    svc.publish(staging, yes=True)
    assert (base / "targets" / identity.target_id / "deployments" / "d1").is_dir()
    assert (base / "targets" / identity.target_id / "migration.json").is_file()
    assert (base / "remotes" / "dev" / "deployments" / "d1").is_dir()

    blocked = StateMigrationService(base).plan({"dev": identity})
    # Force a blocked plan by introducing conflicting alias histories.
    _write_manifest(base / "remotes" / "prod" / "deployments", "d1", body="conflict")
    blocked = StateMigrationService(base).plan(
        {
            "dev": identity,
            "prod": identity,
        }
    )
    assert blocked.blocked
    with pytest.raises(PolicyError):
        svc.stage(blocked, tmp_path / "s2")
