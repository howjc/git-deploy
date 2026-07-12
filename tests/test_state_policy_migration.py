"""Managed policy migration plan/execute tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.errors import PolicyError
from git_deploy.expected_state import ExpectedStateStore, build_expected_state
from git_deploy.state_policy_migration import PolicyMigrationService
from git_deploy.target_identity import resolve_target_identity


def test_policy_plan_lists_paths_and_blocks_normal_deploy(tmp_path: Path) -> None:
    """Plan lists old/new managed paths; normal deploy remains blocked flag."""

    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv")
    root = tmp_path / "t"
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="old-pol",
    )
    store.cas_advance(expected_generation=None, state=state)
    svc = PolicyMigrationService(root, identity)
    plan = svc.plan(
        new_policy="new-pol",
        old_managed_paths=("a.txt",),
        new_managed_paths=("a.txt", "b.txt"),
    )
    assert plan.old_policy == "old-pol"
    assert plan.new_policy == "new-pol"
    assert plan.blocked_for_normal_deploy is True
    assert "b.txt" in plan.readonly_verify_paths


def test_policy_execute_cas_zero_remote_writes(tmp_path: Path) -> None:
    """Execute advances generation via CAS with remote write counter remaining 0."""

    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, "demo", remote_root="/srv")
    root = tmp_path / "t"
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="old-pol",
    )
    store.cas_advance(expected_generation=None, state=state)
    svc = PolicyMigrationService(root, identity)
    plan = svc.plan(new_policy="new-pol")
    counter = [0]
    state_id = svc.execute(plan, remote_write_counter=counter, yes=True)
    assert counter[0] == 0
    current = store.read_current()
    assert current is not None and current.generation == 2
    assert store.read_state(state_id).policy_fingerprint == "new-pol"

    with pytest.raises(PolicyError, match="--yes"):
        svc.execute(plan, yes=False)
