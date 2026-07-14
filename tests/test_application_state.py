"""Read-only application state inspection service tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from git_deploy.application import (
    ApplicationConfigService,
    ApplicationError,
    SideEffectLevel,
    StateAction,
    StateInspectService,
    StateRequest,
)
from git_deploy.expected_state import (
    ExpectedStateStore,
    build_expected_state,
)
from git_deploy.target_identity import default_state_base
from git_deploy.transaction import TransactionStore


def _fixture(tmp_path: Path):
    """Create one generation and one open transaction for inspection."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "state.example.invalid"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = "{tmp_path / 'state'}"
""".strip(),
        encoding="utf-8",
    )
    config = ApplicationConfigService.from_path(config_path)
    selection = config.resolve_project("default", "demo")
    _alias, _server, project, identity = config._resolve_domain_project("default", "demo")
    target_root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    store = ExpectedStateStore(target_root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree-example",
        applied_transition_ids=("sha1:commit:parent",),
        physical_fingerprint=selection.physical_fingerprint,
        policy_fingerprint=selection.policy_fingerprint,
    )
    pointer = store.cas_advance(expected_generation=None, state=state)
    TransactionStore(target_root).create(
        target_id=identity.target_id,
        before_generation=1,
        after_generation=2,
    )
    return config, selection, identity, target_root, store, pointer


def test_application_state_inspect_returns_current_policy_and_transactions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Return structured local state while all persistence methods are blocked."""

    config, selection, identity, _target_root, _store, pointer = _fixture(tmp_path)

    def forbidden(*_args, **_kwargs):
        """Fail if inspect attempts a persisted mutation."""

        raise AssertionError("state inspect attempted a write")

    monkeypatch.setattr(ExpectedStateStore, "write_state", forbidden)
    monkeypatch.setattr(ExpectedStateStore, "cas_advance", forbidden)
    monkeypatch.setattr(TransactionStore, "create", forbidden)
    monkeypatch.setattr(TransactionStore, "advance", forbidden)
    result = StateInspectService(config).inspect(
        StateRequest(
            remote="default",
            project="demo",
            side_effect=SideEffectLevel.LOCAL_READ,
            expected_target_id=identity.target_id,
            expected_physical_fingerprint=selection.physical_fingerprint,
            expected_generation=1,
            action=StateAction.INSPECT,
        )
    )

    assert result.current_present is True
    assert result.generation == 1
    assert result.state_id == pointer.state_id
    assert result.source_tree_id == "tree-example"
    assert result.configured_policy_fingerprint == result.state_policy_fingerprint
    assert result.applied_transition_count == 1
    assert len(result.open_transactions) == 1
    assert result.open_transactions[0].stage == "prepared"


def test_application_state_inspect_maps_corrupt_state_to_structured_error(
    tmp_path: Path,
) -> None:
    """Expose stable error code/category instead of leaking parser internals."""

    config, selection, identity, _target_root, store, pointer = _fixture(tmp_path)
    path = store._state_path(pointer.state_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_tree_id"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ApplicationError) as captured:
        StateInspectService(config).inspect(
            StateRequest(
                remote="default",
                project="demo",
                side_effect=SideEffectLevel.LOCAL_READ,
                expected_target_id=identity.target_id,
                expected_physical_fingerprint=selection.physical_fingerprint,
                expected_generation=1,
                action=StateAction.INSPECT,
            )
        )

    assert captured.value.code == "state.corrupt"
    assert captured.value.to_dict()["category"] == "configuration"
