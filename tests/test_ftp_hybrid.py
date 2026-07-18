"""FTP In-place Hybrid capability, MLSD scan, and Pending contract tests."""

from __future__ import annotations

from dataclasses import replace
import ftplib
import json
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from git_deploy.config import OutputConfig, TargetConfig
from git_deploy.errors import DeployError, PlanError
from git_deploy.ftp_hybrid import (
    FTP_HYBRID_SCHEMA,
    FTPHybridCapabilities,
    FTPHybridPending,
    FTPPendingPhase,
    capability_profile_path,
    load_capability_profile,
    local_manifest_hash,
    parse_capabilities,
    parse_pending,
    save_capability_profile,
    scan_ftp_tree,
    serialize_capabilities,
    serialize_pending,
    validate_pending_resume,
)
from git_deploy.hybrid import scan_hybrid_output
from git_deploy.manifest import ManifestEntry, TargetState
from git_deploy.transports.ftp import FTPTransport


def _target() -> TargetConfig:
    """Return one deterministic FTP target for schema and adapter tests."""

    return TargetConfig(
        "prod",
        "ftp",
        "ftp.example.invalid",
        "deploy",
        PurePosixPath("/root"),
        21,
        password_env="FTP_PASSWORD",
    )


def _manifest(tmp_path: Path):  # noqa: ANN202
    """Create and scan one local Hybrid view with content and an empty directory."""

    root = tmp_path / "aggregation"
    (root / "assets/empty").mkdir(parents=True)
    (root / "index.html").write_bytes(b"index")
    (root / "assets/app.js").write_bytes(b"app")
    return scan_hybrid_output(
        OutputConfig(root, PurePosixPath("."), name="frontend-root", mode="hybrid")
    )


def test_capability_profile_round_trip_atomic_store_and_staleness(tmp_path: Path) -> None:
    """Profiles are strict, non-secret, atomic, and bound to target plus banner."""

    target = _target()
    banner = "a" * 64
    profile = FTPHybridCapabilities(
        FTP_HYBRID_SCHEMA,
        target.fingerprint,
        banner,
        True,
        True,
        True,
        True,
        True,
        True,
        123,
    )
    data = serialize_capabilities(profile)
    assert parse_capabilities(data) == profile
    path = save_capability_profile(tmp_path, profile)
    assert path == capability_profile_path(tmp_path, target)
    assert load_capability_profile(tmp_path, target, server_banner_hash=banner) == profile
    assert "FTP_PASSWORD" not in data.decode("utf-8")

    with pytest.raises(PlanError, match="banner changed"):
        load_capability_profile(tmp_path, target, server_banner_hash="b" * 64)
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(PlanError, match="valid UTF-8 JSON"):
        load_capability_profile(tmp_path, target, server_banner_hash=banner)


def test_capability_profile_rejects_missing_feature_and_target_change(tmp_path: Path) -> None:
    """A partial proof or different endpoint never silently enables FTP Hybrid."""

    target = _target()
    partial = FTPHybridCapabilities(
        1,
        target.fingerprint,
        "c" * 64,
        True,
        True,
        False,
        True,
        True,
        True,
        1,
    )
    save_capability_profile(tmp_path, partial)
    with pytest.raises(PlanError, match="does not satisfy"):
        load_capability_profile(tmp_path, target, server_banner_hash="c" * 64)

    changed = replace(target, port=2121)
    with pytest.raises(PlanError, match="missing"):
        load_capability_profile(tmp_path, changed, server_banner_hash="c" * 64)


def test_pending_round_trip_phases_identity_and_resume_guards(tmp_path: Path) -> None:
    """Pending embeds frozen State and fails closed on every identity mismatch."""

    target = _target()
    manifest_hash = local_manifest_hash(_manifest(tmp_path))
    state = TargetState(
        1,
        target.name,
        target.fingerprint,
        "head-1",
        10,
        {"index.html": ManifestEntry("d" * 64, 5)},
    )
    pending = FTPHybridPending(
        1,
        "github.com/acme/project",
        "frontend-root",
        ".",
        target.fingerprint,
        "deployment-1",
        FTPPendingPhase.PREPARED,
        "1" * 64,
        "2" * 64,
        manifest_hash,
        "head-1",
        state,
        11,
    )
    parsed = parse_pending(
        serialize_pending(pending),
        project_id=pending.project_id,
        mapping=pending.mapping,
        remote=pending.remote,
        target=target,
    )
    assert parsed == pending
    assert parsed.with_phase(FTPPendingPhase.FILES_PUBLISHED).phase is FTPPendingPhase.FILES_PUBLISHED
    validate_pending_resume(
        parsed,
        manifest_hash=manifest_hash,
        head="head-1",
        current_ownership_hash="1" * 64,
    )
    validate_pending_resume(
        parsed,
        manifest_hash=manifest_hash,
        head="head-1",
        current_ownership_hash="2" * 64,
    )

    with pytest.raises(PlanError, match="Manifest"):
        validate_pending_resume(
            parsed,
            manifest_hash="3" * 64,
            head="head-1",
            current_ownership_hash="1" * 64,
        )
    with pytest.raises(PlanError, match="HEAD"):
        validate_pending_resume(
            parsed,
            manifest_hash=manifest_hash,
            head="head-2",
            current_ownership_hash="1" * 64,
        )
    with pytest.raises(PlanError, match="Ownership"):
        validate_pending_resume(
            parsed,
            manifest_hash=manifest_hash,
            head="head-1",
            current_ownership_hash="4" * 64,
        )
    with pytest.raises(PlanError, match="identity"):
        parse_pending(
            serialize_pending(pending),
            project_id="github.com/other/project",
            mapping=pending.mapping,
            remote=pending.remote,
            target=target,
        )

    corrupt = json.loads(serialize_pending(pending))
    corrupt["phase"] = "ROLLED_BACK"
    with pytest.raises(PlanError, match="unknown phase"):
        parse_pending(
            json.dumps(corrupt).encode(),
            project_id=pending.project_id,
            mapping=pending.mapping,
            remote=pending.remote,
            target=target,
        )
    with pytest.raises(PlanError, match="valid UTF-8 JSON"):
        parse_pending(
            b"not-json",
            project_id=pending.project_id,
            mapping=pending.mapping,
            remote=pending.remote,
            target=target,
        )


class FakeMLSDSession:
    """Expose deterministic FEAT, MLSD, and RETR responses to FTPTransport."""

    def __init__(self) -> None:
        """Create a typed tree containing files, directories, and an empty leaf."""

        self.listings = {
            "/root": [("assets", {"type": "dir", "modify": "20260718120000"})],
            "/root/assets": [
                ("app.js", {"type": "file", "size": "3"}),
                ("empty", {"type": "dir"}),
                ("nested", {"type": "dir"}),
            ],
            "/root/assets/empty": [],
            "/root/assets/nested": [("x.css", {"type": "file", "size": "1"})],
        }
        self.files = {
            "/root/assets/app.js": b"app",
            "/root/assets/nested/x.css": b"x",
        }

    def mlsd(self, path: str):  # noqa: ANN201
        """Return configured MLSD rows or one permanent listing failure."""

        if path not in self.listings:
            raise DeployError(f"missing listing: {path}")
        return iter(self.listings[path])

    def retrbinary(self, command: str, callback) -> None:  # noqa: ANN001
        """Stream one configured file in a single binary block."""

        callback(self.files[command.removeprefix("RETR ")])

    def sendcmd(self, command: str) -> str:
        """Return a multiline feature response with MLST semantics."""

        assert command == "FEAT"
        return "211-Features\n MLST type*;size*;modify*;\n UTF8\n211 End"

    def getwelcome(self) -> str:
        """Return one stable non-secret server greeting."""

        return "220 Fixture FTP"


def test_typed_mlsd_scanner_bounded_read_and_cache() -> None:
    """MLSD drives stable recursion, empty directories, FEAT, and bounded RETR."""

    transport = FTPTransport(_target())
    session = FakeMLSDSession()
    transport.ftp = cast(Any, session)

    assert transport.features() == frozenset({"MLST", "MLSD", "UTF8"})
    assert transport.server_banner_hash() == transport.server_banner_hash()
    tree = scan_ftp_tree(transport, "assets")
    assert tree.files == ("app.js", "nested/x.css")
    assert tree.directories == ("empty", "nested")
    assert transport.read_file("assets/app.js", max_bytes=3) == b"app"
    with pytest.raises(DeployError, match="exceeds 2 byte"):
        transport.read_file("assets/app.js", max_bytes=2)


@pytest.mark.parametrize("remote_type", ["OS.unix=symlink", "unknown", ""])
def test_typed_mlsd_rejects_unknown_and_symlink_types(remote_type: str) -> None:
    """No server-specific MLSD type may be followed or treated as a file."""

    transport = FTPTransport(_target())
    session = FakeMLSDSession()
    session.listings["/root"] = [("unsafe", {"type": remote_type})]
    transport.ftp = cast(Any, session)
    with pytest.raises(DeployError, match="unsupported type"):
        transport.list_directory_typed(".")


def test_typed_scanner_enforces_depth_and_count() -> None:
    """Recursive MLSD scanning stops before unbounded remote traversal."""

    transport = FTPTransport(_target())
    transport.ftp = cast(Any, FakeMLSDSession())
    with pytest.raises(PlanError, match="depth 0"):
        scan_ftp_tree(transport, "assets", max_depth=0)

    transport = FTPTransport(_target())
    transport.ftp = cast(Any, FakeMLSDSession())
    with pytest.raises(PlanError, match="exceeds 1 entries"):
        scan_ftp_tree(transport, "assets", max_entries=1)


def test_typed_mlsd_permission_failure_is_not_missing_or_empty() -> None:
    """A permanent MLSD access error propagates instead of authorizing deletion."""

    class DeniedSession(FakeMLSDSession):
        """Reject every MLSD command with an explicit permission response."""

        def mlsd(self, path: str):  # noqa: ANN201
            """Raise the server's permanent access denial."""

            raise ftplib.error_perm("550 Permission denied")

    transport = FTPTransport(_target())
    transport.ftp = cast(Any, DeniedSession())
    with pytest.raises(DeployError, match="MLSD failed.*Permission denied"):
        transport.list_directory_typed(".")
