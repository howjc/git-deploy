"""Doctor application contract and read-only scheduler tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess

import pytest

from git_deploy.application import (
    ApplicationConfigService,
    DoctorCheckCategory,
    DoctorCheckResult,
    DoctorCheckStatus,
    DoctorRequest,
    DoctorService,
    SideEffectLevel,
    standard_doctor_service,
)


def _config(tmp_path: Path) -> ApplicationConfigService:
    """Create a minimal parsed configuration for scheduler tests."""

    repository = tmp_path / "repository"
    repository.mkdir()
    path = tmp_path / "deploy.toml"
    path.write_text(
        f"""
[server]
protocol = "sftp"
host = "doctor.example.invalid"

[projects.application]
repository = "{repository}"
remote_root = "/srv/application"
""".strip(),
        encoding="utf-8",
    )
    return ApplicationConfigService.from_path(path)


def _result(
    check_id: str,
    category: DoctorCheckCategory,
    status: DoctorCheckStatus = DoctorCheckStatus.PASS,
) -> DoctorCheckResult:
    """Create one compact check result."""

    return DoctorCheckResult.create(
        check_id=check_id,
        category=category,
        status=status,
        summary=check_id,
        side_effect=(
            SideEffectLevel.REMOTE_READ
            if category is DoctorCheckCategory.REMOTE
            else SideEffectLevel.LOCAL_READ
        ),
    )


def test_doctor_contract_is_immutable_serializable_and_redacted() -> None:
    """Freeze checks and remove secret values/references before serialization."""

    sentinel = "AUTH-SENTINEL"
    check = DoctorCheckResult.create(
        check_id="local.credentials",
        category=DoctorCheckCategory.LOCAL,
        status=DoctorCheckStatus.WARN,
        summary="credential variable is not configured",
        side_effect=SideEffectLevel.LOCAL_READ,
        context={"password": sentinel, "reference": "op://vault/item/field"},
        suggested_action="set the named environment variable",
    )

    with pytest.raises(FrozenInstanceError):
        check.summary = "changed"  # type: ignore[misc]
    payload = check.to_dict()
    assert sentinel not in repr(payload)
    assert "op://" not in repr(payload)
    assert payload["context"] == {"password": "***", "reference": "***"}


def test_doctor_scheduler_continues_and_remote_checks_are_opt_in(tmp_path: Path) -> None:
    """Continue after one failure and never run remote checks by default."""

    calls = {"local": 0, "state": 0, "remote": 0}

    def local(_request: DoctorRequest) -> DoctorCheckResult:
        """Raise a safe diagnostic failure."""

        calls["local"] += 1
        raise ValueError("broken local input")

    def state(_request: DoctorRequest) -> DoctorCheckResult:
        """Return an independent state result after the local failure."""

        calls["state"] += 1
        return _result("state.current", DoctorCheckCategory.STATE)

    def remote(_request: DoctorRequest) -> DoctorCheckResult:
        """Count explicit remote-read scheduling."""

        calls["remote"] += 1
        return _result("remote.connect", DoctorCheckCategory.REMOTE)

    service = DoctorService(
        _config(tmp_path),
        local_checks=(local,),
        state_checks=(state,),
        remote_checks=(remote,),
    )
    local_report = service.run(DoctorRequest(remote=None, target="application"))
    remote_report = service.run(
        DoctorRequest(remote=None, target="application", check_remote=True)
    )

    assert calls == {"local": 2, "state": 2, "remote": 1}
    assert [item.status for item in local_report.checks] == [
        DoctorCheckStatus.FAIL,
        DoctorCheckStatus.PASS,
    ]
    assert local_report.ready_label == "NOT READY"
    assert remote_report.checks[-1].side_effect is SideEffectLevel.REMOTE_READ
    assert remote_report.to_dict()["schema_version"] == 1


def test_standard_local_checks_report_safe_config_selection_and_git(
    tmp_path: Path,
) -> None:
    """Report config fields and Git metadata without exposing plaintext values."""

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "doctor@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Doctor"],
        check=True,
    )
    (repository / "app.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "app.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "initial"], check=True
    )
    sentinel = "DOCTOR-PASSWORD-SENTINEL"
    path = tmp_path / "deploy.toml"
    path.write_text(
        f"""
[server]
protocol = "sftp"
host = "doctor.example.invalid"
password = "{sentinel}"

[projects.application]
repository = "{repository}"
remote_root = "/srv/application"
""".strip(),
        encoding="utf-8",
    )
    config = ApplicationConfigService.from_path(path)
    report = standard_doctor_service(config).run(
        DoctorRequest(remote=None, target="application")
    )

    serialized = repr(report.to_dict())
    assert [item.check_id for item in report.checks[:3]] == [
        "local.configuration",
        "local.selection",
        "local.git",
    ]
    assert report.checks[0].status is DoctorCheckStatus.WARN
    assert "server.password" in serialized
    assert sentinel not in serialized
    assert report.checks[2].status is DoctorCheckStatus.PASS


def test_state_checks_validate_current_and_surface_silently_ignored_corruption(
    tmp_path: Path,
) -> None:
    """Read valid current/CAS/tree and report every corrupt manifest or journal."""

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, policy_fingerprint_for_project

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "doctor@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Doctor"],
        check=True,
    )
    body = b"healthy\n"
    (repository / "app.txt").write_bytes(body)
    subprocess.run(["git", "-C", str(repository), "add", "app.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "initial"], check=True
    )
    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = tmp_path / "deploy.toml"
    path.write_text(
        f"""
[server]
protocol = "sftp"
host = "doctor.example.invalid"

[projects.application]
repository = "{repository}"
remote_root = "/srv/application"
local_state_dir = "{tmp_path / 'state'}"
""".strip(),
        encoding="utf-8",
    )
    config = ApplicationConfigService.from_path(path)
    selection = config.resolve_project(None, "application")
    _alias, _server, project, identity = config._resolve_domain_project(
        None, "application"
    )
    root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    digest = ContentAddressedStore(root).put(body)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(),
        physical_fingerprint=selection.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry("app.txt", "source", digest),),
    )
    ExpectedStateStore(root, identity).cas_advance(
        expected_generation=None,
        state=state,
    )
    broken_manifest = root / "deployments" / "broken-record" / "manifest.json"
    broken_manifest.parent.mkdir(parents=True)
    broken_manifest.write_text("{not-json", encoding="utf-8")
    broken_journal = root / "transactions" / "broken.json"
    broken_journal.parent.mkdir(parents=True)
    broken_journal.write_text("{not-json", encoding="utf-8")
    # P1-04: a rollback recovery directory has no manifest.json by design and
    # must not be misreported as a corrupt deployment record.
    from git_deploy.state import DeploymentStore

    DeploymentStore(project, root=root).write_backup(
        "rb-some-deployment", 0, b"pre-rollback-bytes"
    )
    before = {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in root.rglob("*")
        if item.is_file()
    }

    remote = InMemoryTransport()
    remote.files["/srv/application/app.txt"] = FakeRemotePath(body)
    set_cli_transport_factory(lambda _server: remote)
    try:
        report = standard_doctor_service(config).run(
            DoctorRequest(remote=None, target="application", check_remote=True)
        )
    finally:
        set_cli_transport_factory(None)
    by_id = {item.check_id: item for item in report.checks}
    after = {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in root.rglob("*")
        if item.is_file()
    }

    assert by_id["state.current"].status is DoctorCheckStatus.PASS
    assert by_id["state.manifests"].status is DoctorCheckStatus.FAIL
    assert "broken-record" in by_id["state.manifests"].summary
    assert "rb-some-deployment" not in by_id["state.manifests"].summary
    manifests_context = by_id["state.manifests"].to_dict()["context"]
    assert manifests_context["rollback_event_counts"] == (("application", 1),)
    assert by_id["state.transactions"].status is DoctorCheckStatus.FAIL
    assert "broken.json" in by_id["state.transactions"].summary
    assert by_id["remote.access"].status is DoctorCheckStatus.PASS
    assert remote.write_calls == 0
    assert hashlib.sha256(body).hexdigest() == digest
    assert after == before
