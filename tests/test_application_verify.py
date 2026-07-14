"""Local and remote-read application verification service tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from git_deploy.application import (
    ApplicationConfigService,
    SideEffectLevel,
    VerifyMode,
    VerifyRequest,
    VerifyService,
)
from git_deploy.expected_state import ExpectedStateStore
from git_deploy.models import DeploymentManifest, FileSnapshot
from git_deploy.state import DeploymentStore
from git_deploy.target_identity import default_state_base


class FakeReadableTransport:
    """In-memory transport exposing read/write counters for verification."""

    supports_commands = False

    def __init__(self, files: dict[str, bytes]):
        """Initialize exact remote file bytes."""

        self.files = files
        self.read_calls = 0
        self.write_calls = 0
        self.closed = False

    def read_file(self, path: str, progress=None) -> bytes | None:
        """Read one path and increment the read counter."""

        self.read_calls += 1
        return self.files.get(path)

    def close(self) -> None:
        """Record transport closure."""

        self.closed = True


def _fixture(tmp_path: Path):
    """Create one deployment manifest and its application configuration."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "verify.example.invalid"

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
    content = b"verified\n"
    manifest = DeploymentManifest(
        deployment_id="20260713-verify",
        project="demo",
        repository=str(repository),
        remote_root="/srv/demo",
        created_at="2026-07-13T00:00:00Z",
        status="succeeded",
        from_commit="a",
        to_commit="b",
        snapshots=[
            FileSnapshot(
                path="app.txt",
                remote_path="/srv/demo/app.txt",
                before_exists=False,
                before_sha256=None,
                backup_file=None,
                after_exists=True,
                after_sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    DeploymentStore(project, root=target_root).write_manifest(manifest)
    return config, selection, identity, content


def test_application_verify_local_mode_never_opens_transport(tmp_path: Path) -> None:
    """Expose local expectations without constructing a remote transport."""

    config, selection, identity, _content = _fixture(tmp_path)

    def forbidden(_values):
        """Fail if local mode opens a transport."""

        raise AssertionError("local verify opened a transport")

    result = VerifyService(config, transport_factory=forbidden).verify(
        VerifyRequest(
            remote="default",
            project="demo",
            side_effect=SideEffectLevel.LOCAL_READ,
            expected_target_id=identity.target_id,
            expected_physical_fingerprint=selection.physical_fingerprint,
            expected_generation=None,
            latest=True,
            remote_check=False,
        )
    )

    assert result.mode is VerifyMode.LOCAL
    assert result.paths[0].status == "unverified"
    assert result.remote_read_calls == 0
    assert result.remote_write_calls == 0


def test_application_verify_remote_mode_is_strictly_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Compare remote bytes while transport and state write counters remain zero."""

    config, selection, identity, content = _fixture(tmp_path)
    transport = FakeReadableTransport({"/srv/demo/app.txt": content})

    def forbidden(*_args, **_kwargs):
        """Fail if verification attempts state mutation."""

        raise AssertionError("verify attempted a state write")

    monkeypatch.setattr(ExpectedStateStore, "write_state", forbidden)
    monkeypatch.setattr(ExpectedStateStore, "cas_advance", forbidden)
    result = VerifyService(
        config,
        transport_factory=lambda _values: transport,  # type: ignore[arg-type]
    ).verify(
        VerifyRequest(
            remote="default",
            project="demo",
            side_effect=SideEffectLevel.REMOTE_READ,
            expected_target_id=identity.target_id,
            expected_physical_fingerprint=selection.physical_fingerprint,
            expected_generation=None,
            latest=True,
            remote_check=True,
        )
    )

    assert result.mode is VerifyMode.REMOTE_READ
    assert result.ok is True
    assert result.paths[0].status == "match"
    assert result.remote_read_calls == 1
    assert result.remote_write_calls == 0
    assert transport.write_calls == 0
    assert transport.closed is True
