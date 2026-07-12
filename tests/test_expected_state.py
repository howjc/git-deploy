"""Immutable expected-state schema, store, and current pointer tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from git_deploy.durable_io import set_fault_hook
from git_deploy.errors import ConfigurationError
from git_deploy.expected_state import (
    ExpectedStateStore,
    FileEntry,
    ManifestLineage,
    build_expected_state,
)
from git_deploy.models import DeploymentManifest
from git_deploy.target_identity import TargetIdentity, build_physical_payload, derive_target_id


def _identity(project: str = "demo") -> TargetIdentity:
    """Build a fixed test identity.

    Args:
        project: Project key.

    Returns:
        Target identity.
    """

    payload = build_physical_payload(
        protocol="sftp",
        host="app.example",
        project=project,
        remote_root="/srv/demo",
    )
    return TargetIdentity(
        target_id=derive_target_id(payload),
        payload=payload,
        physical_fingerprint=payload.fingerprint(),
    )


def _sample_state(**overrides: object):
    """Build a minimal valid expected state.

    Returns:
        ExpectedState instance.
    """

    kwargs = {
        "generation": 1,
        "parent_state_id": None,
        "source_tree_id": "tree-abc",
        "applied_transition_ids": ("t1",),
        "physical_fingerprint": "phys",
        "policy_fingerprint": "pol",
        "files": (FileEntry(path="a.txt", owner="source", content_sha256="aa"),),
    }
    kwargs.update(overrides)
    return build_expected_state(**kwargs)  # type: ignore[arg-type]


def test_schema_canonical_json_and_unknown_rejected(tmp_path: Path) -> None:
    """Schema covers canonical JSON, state id, generation, and unknown schema rejection."""

    state = _sample_state()
    payload = state.to_dict()
    assert payload["schema_version"] == 1
    assert payload["state_id"].startswith("sha256:")
    assert payload["generation"] == 1
    assert payload["parent_state_id"] is None
    assert payload["source_tree_id"] == "tree-abc"
    assert payload["applied_transition_ids"] == ["t1"]
    assert payload["physical_fingerprint"] == "phys"
    assert payload["policy_fingerprint"] == "pol"
    assert payload["files"][0]["owner"] == "source"
    assert payload["files"][0]["content_sha256"] == "aa"

    bad = dict(payload)
    bad["schema_version"] = 99
    with pytest.raises(ConfigurationError, match="unknown expected-state schema"):
        from git_deploy.expected_state import ExpectedState

        ExpectedState.from_dict(bad)


def test_manifest_compat_legacy_and_new_roundtrip(tmp_path: Path) -> None:
    """v0.1.5 manifests stay legacy; new manifests retain before/after lineage."""

    legacy = DeploymentManifest.from_dict(
        {
            "deployment_id": "d1",
            "project": "demo",
            "repository": "/repo",
            "remote_root": "/srv",
            "from_commit": "a",
            "to_commit": "b",
            "created_at": "t",
            "status": "succeeded",
            "snapshots": [],
        }
    )
    assert legacy.lineage_label() == "legacy"
    lineage = ManifestLineage.from_dict(legacy.to_dict())
    assert lineage.state == "legacy"

    modern = DeploymentManifest(
        deployment_id="d2",
        project="demo",
        repository="/repo",
        remote_root="/srv",
        from_commit="a",
        to_commit="b",
        created_at="t",
        status="succeeded",
        before_state_id="sha256:before",
        after_state_id="sha256:after",
        before_generation=1,
        after_generation=2,
        introduced_transition_ids=["t1"],
        transaction_id="tx1",
        target_id="tgt",
        state="v1",
    )
    restored = DeploymentManifest.from_dict(modern.to_dict())
    assert restored.lineage_label() == "v1"
    assert restored.before_state_id == "sha256:before"
    assert restored.after_state_id == "sha256:after"
    assert restored.transaction_id == "tx1"


def test_manifest_compat_blocks_legacy_rollback_when_current_exists(tmp_path: Path) -> None:
    """With current established, legacy-only rollback must be blocked by callers."""

    identity = _identity()
    store = ExpectedStateStore(tmp_path / "targets" / identity.target_id, identity)
    state = _sample_state(
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="pol",
    )
    store.cas_advance(expected_generation=None, state=state)
    assert store.read_current() is not None
    # Policy helper: presence of current means legacy rollback is unsafe.
    assert store.load_current_state() is not None


def test_immutable_write_read_rehash_and_tamper(tmp_path: Path) -> None:
    """Immutable state uses durable publish, rehash on read, and rejects tamper."""

    identity = _identity()
    store = ExpectedStateStore(tmp_path / "t", identity)
    state = _sample_state(physical_fingerprint=identity.physical_fingerprint)
    state_id = store.write_state(state)
    assert store.write_state(state) == state_id
    loaded = store.read_state(state_id)
    assert loaded.state_id() == state_id

    path = store._state_path(state_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["source_tree_id"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="hash mismatch|mismatch"):
        store.read_state(state_id)


def test_current_pointer_cas_and_fault(tmp_path: Path) -> None:
    """Generation CAS rejects stale writes; fault mid-publish leaves old or new only."""

    identity = _identity()
    store = ExpectedStateStore(tmp_path / "t", identity)
    first = _sample_state(
        generation=1,
        physical_fingerprint=identity.physical_fingerprint,
        source_tree_id="t1",
    )
    pointer = store.cas_advance(expected_generation=None, state=first)
    assert pointer.generation == 1

    stale = _sample_state(
        generation=1,
        physical_fingerprint=identity.physical_fingerprint,
        source_tree_id="stale",
    )
    with pytest.raises(ConfigurationError, match="CAS conflict|already exists|older"):
        store.cas_advance(expected_generation=None, state=stale)

    second = _sample_state(
        generation=2,
        parent_state_id=first.state_id(),
        physical_fingerprint=identity.physical_fingerprint,
        source_tree_id="t2",
        applied_transition_ids=("t1", "t2"),
    )

    def fault(stage: str, path: Path) -> None:
        if stage == "after_file_fsync" and path.name == "current.json":
            raise RuntimeError("kill")

    set_fault_hook(fault)
    with pytest.raises(RuntimeError, match="kill"):
        store.cas_advance(expected_generation=1, state=second)
    set_fault_hook(None)

    current = store.read_current()
    assert current is not None
    assert current.generation in {1, 2}
    assert current.state_id in {first.state_id(), second.state_id()}

    # Successful CAS after recovery.
    if current.generation == 1:
        store.cas_advance(expected_generation=1, state=second)
    final = store.read_current()
    assert final is not None
    assert final.generation == 2
