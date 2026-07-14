"""Read-only application history service tests."""

from __future__ import annotations

from pathlib import Path

from git_deploy.application import (
    ApplicationConfigService,
    HistoryLineage,
    HistoryRequest,
    HistoryService,
    SideEffectLevel,
)
from git_deploy.models import DeploymentManifest
from git_deploy.state import DeploymentStore
from git_deploy.target_identity import default_state_base


def _manifest(deployment_id: str, *, stateful: bool) -> DeploymentManifest:
    """Return a minimal legacy or stateful deployment fixture."""

    return DeploymentManifest(
        deployment_id=deployment_id,
        project="demo",
        repository="/fixture",
        remote_root="/srv/demo",
        created_at="2026-07-13T00:00:00Z",
        status="succeeded",
        from_commit="a",
        to_commit="b",
        revision_specs=["HEAD"],
        state="v1" if stateful else "legacy",
        before_generation=1 if stateful else None,
        after_generation=2 if stateful else None,
        transaction_id="txn-stateful" if stateful else None,
    )


def _fixture(tmp_path: Path):
    """Create configuration plus target-scoped and legacy history records."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "history.example.invalid"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = "{tmp_path / 'state'}"
""".strip(),
        encoding="utf-8",
    )
    config = ApplicationConfigService.from_path(config_path)
    _alias, _server, project, identity = config._resolve_domain_project("default", "demo")
    target_root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    DeploymentStore(project).write_manifest(_manifest("20260713-legacy", stateful=False))
    DeploymentStore(project, root=target_root).write_manifest(
        _manifest("20260714-stateful", stateful=True)
    )
    return config, project, identity


def test_application_history_pages_selects_and_distinguishes_lineage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Page merged local history without transport calls or state writes."""

    config, project, identity = _fixture(tmp_path)
    selection = config.resolve_project("default", "demo")
    target_root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    corrupt = target_root / "deployments" / "20260715-corrupt" / "manifest.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not-json", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        """Fail if a history read attempts a persisted write."""

        raise AssertionError("history service attempted a write")

    monkeypatch.setattr(DeploymentStore, "write_manifest", forbidden)
    monkeypatch.setattr(DeploymentStore, "write_backup", forbidden)
    service = HistoryService(config)
    common = {
        "remote": "default",
        "project": "demo",
        "side_effect": SideEffectLevel.LOCAL_READ,
        "expected_target_id": identity.target_id,
        "expected_physical_fingerprint": selection.physical_fingerprint,
        "expected_generation": None,
    }

    first = service.history(HistoryRequest(**common, limit=1))
    second = service.history(HistoryRequest(**common, limit=1, offset=1))
    selected = service.history(
        HistoryRequest(**common, deployment_id="20260713-leg")
    )

    assert first.total == 2
    assert first.next_offset == 1
    assert first.entries[0].lineage is HistoryLineage.STATEFUL
    assert second.next_offset is None
    assert second.entries[0].lineage is HistoryLineage.LEGACY
    assert selected.entries[0].deployment_id == "20260713-legacy"
    assert first.corrupt_records == (str(corrupt),)
