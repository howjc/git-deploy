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
    FTP_CAPABILITY_SCHEMA,
    FTP_PENDING_SCHEMA,
    FTPHybridCapabilities,
    FTPHybridPending,
    FTPPendingPhase,
    capability_profile_path,
    load_capability_profile,
    local_manifest_hash,
    parse_capabilities,
    parse_pending,
    pending_local_manifest_hash,
    pending_manifest_outputs,
    probe_ftp_hybrid_capabilities,
    save_capability_profile,
    scan_ftp_tree,
    serialize_capabilities,
    serialize_pending,
    validate_pending_resume,
    validate_remote_root_aliases,
)
from git_deploy.hybrid import hybrid_content_manifest, scan_hybrid_output
from git_deploy.manifest import ManifestEntry, TargetState
from git_deploy.transports.base import RemotePathType
from git_deploy.transports.ftp import FTPRemoteEntry, FTPTransport


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


def test_capability_profile_round_trip_atomic_store_and_staleness(
    tmp_path: Path,
) -> None:
    """Profiles are strict, non-secret, atomic, and bound to target plus banner."""

    target = _target()
    banner = "a" * 64
    profile = FTPHybridCapabilities(
        FTP_CAPABILITY_SCHEMA,
        target.fingerprint,
        banner,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        123,
        True,
        True,
        True,
    )
    data = serialize_capabilities(profile)
    assert parse_capabilities(data) == profile
    path = save_capability_profile(tmp_path, profile)
    assert path == capability_profile_path(tmp_path, target)
    assert (
        load_capability_profile(tmp_path, target, server_banner_hash=banner) == profile
    )
    assert "FTP_PASSWORD" not in data.decode("utf-8")

    with pytest.raises(PlanError, match="banner changed"):
        load_capability_profile(tmp_path, target, server_banner_hash="b" * 64)
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(PlanError, match="valid UTF-8 JSON"):
        load_capability_profile(tmp_path, target, server_banner_hash=banner)


def test_capability_profile_rejects_missing_feature_and_target_change(
    tmp_path: Path,
) -> None:
    """A partial proof or different endpoint never silently enables FTP Hybrid."""

    target = _target()
    partial = FTPHybridCapabilities(
        FTP_CAPABILITY_SCHEMA,
        target.fingerprint,
        "c" * 64,
        True,
        True,
        True,
        False,
        True,
        True,
        True,
        1,
        True,
        True,
        True,
    )
    save_capability_profile(tmp_path, partial)
    with pytest.raises(PlanError, match="does not satisfy"):
        load_capability_profile(tmp_path, target, server_banner_hash="c" * 64)

    changed = replace(target, port=2121)
    with pytest.raises(PlanError, match="missing"):
        load_capability_profile(tmp_path, changed, server_banner_hash="c" * 64)


@pytest.mark.parametrize("schema", [1, 2])
def test_old_capability_profile_requires_a_new_probe(
    tmp_path: Path,
    schema: int,
) -> None:
    """Schema 1/2 profiles cannot migrate without proving Unicode semantics."""

    target = _target()
    legacy = {
        "schema": schema,
        "target_fingerprint": target.fingerprint,
        "server_banner_hash": "a" * 64,
        "features": {
            "mlsd": True,
            "retr": True,
            "rename_cross_directory": True,
            "rename_replace_file": True,
            "delete_file": True,
            "remove_directory": True,
            **({"case_sensitive_paths": True} if schema == 2 else {}),
        },
        "probed_at": 1,
    }
    with pytest.raises(PlanError, match="obsolete.*probe"):
        parse_capabilities(json.dumps(legacy).encode())


def test_capability_probe_requires_advertised_utf8() -> None:
    """An ASCII-only server fails before the probe creates any remote path."""

    class MissingUTF8Transport:
        """Advertise MLSD without the mandatory UTF8 feature."""

        def features(self) -> frozenset[str]:
            """Return the incomplete server feature set."""

            return frozenset({"MLSD"})

    with pytest.raises(DeployError, match="mandatory UTF8"):
        probe_ftp_hybrid_capabilities(cast(Any, MissingUTF8Transport()), _target())


@pytest.mark.parametrize(
    "existing,planned",
    (
        ("Assets", "assets"),
        ("Index.html", "index.html"),
        ("cafe\u0301", "caf\u00e9"),
        (".GIT-DEPLOY", ".git-deploy"),
    ),
)
def test_remote_root_alias_gate_rejects_unknown_equivalent_spellings(
    existing: str,
    planned: str,
) -> None:
    """Case and normalization aliases fail before a managed root is touched."""

    transport = FTPTransport(_target())
    session = FakeMLSDSession()
    session.listings["/root"] = [(existing, {"type": "dir"})]
    transport.ftp = cast(Any, session)

    with pytest.raises(PlanError, match="remote root aliases"):
        validate_remote_root_aliases(transport, (("planned", planned),))


def test_remote_root_alias_gate_allows_exact_and_unrelated_unknown_names() -> None:
    """Exact adoption candidates pass while unrelated unknown aliases stay untouched."""

    transport = FTPTransport(_target())
    session = FakeMLSDSession()
    session.listings["/root"] = [
        ("assets", {"type": "dir"}),
        ("Other", {"type": "OS.unix=symlink"}),
        ("other", {"type": "dir"}),
    ]
    transport.ftp = cast(Any, session)

    validate_remote_root_aliases(transport, (("hybrid", "assets"),))
    assert transport.lstat("assets") is RemotePathType.DIRECTORY


def test_capability_probe_alias_gate_is_zero_mutation() -> None:
    """An internal-root alias aborts before the first probe directory mutation."""

    class AliasedInternalTransport:
        """Expose only a conflicting internal root and forbid every mutation."""

        def features(self) -> frozenset[str]:
            """Advertise mandatory features so aliasing is decisive."""

            return frozenset({"MLSD", "UTF8"})

        def enable_utf8(self) -> None:
            """Accept session UTF-8 activation."""

        def list_root_names(self) -> tuple[str, ...]:
            """Return the conflicting unknown root without recursion."""

            return (".GIT-DEPLOY",)

        def make_directory(self, path: str, *, mode: int = 0o755) -> None:
            """Fail if the gate permits any probe mutation."""

            del path, mode
            raise AssertionError("probe mutated before alias rejection")

    with pytest.raises(PlanError, match="remote root aliases"):
        probe_ftp_hybrid_capabilities(cast(Any, AliasedInternalTransport()), _target())


def test_local_hybrid_rejects_root_and_nested_casefold_collisions(
    tmp_path: Path,
) -> None:
    """Local sibling names must remain portable before any remote connection."""

    root = tmp_path / "aggregation"
    root.mkdir()
    (root / "Index.html").write_text("upper", encoding="utf-8")
    (root / "index.html").write_text("lower", encoding="utf-8")
    output = OutputConfig(root, PurePosixPath("."), name="frontend-root", mode="hybrid")
    with pytest.raises(PlanError, match="collide.*Index.html.*index.html"):
        scan_hybrid_output(output)

    (root / "Index.html").unlink()
    (root / "index.html").unlink()
    (root / "assets").mkdir()
    (root / "assets/App.js").write_text("upper", encoding="utf-8")
    (root / "assets/app.js").write_text("lower", encoding="utf-8")
    with pytest.raises(PlanError, match="collide.*App.js.*app.js"):
        scan_hybrid_output(output)


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
        FTP_PENDING_SCHEMA,
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
        "3" * 64,
        "4" * 64,
    )
    parsed = parse_pending(
        serialize_pending(pending),
        project_id=pending.project_id,
        mapping=pending.mapping,
        remote=pending.remote,
        target=target,
    )
    assert parsed == pending
    assert (
        parsed.with_phase(FTPPendingPhase.FILES_PUBLISHED).phase
        is FTPPendingPhase.FILES_PUBLISHED
    )
    validate_pending_resume(
        parsed,
        manifest_hash=manifest_hash,
        head="head-1",
        non_hybrid_plan_hash="3" * 64,
        previous_state_hash="4" * 64,
        current_ownership_hash="1" * 64,
    )
    validate_pending_resume(
        parsed.with_phase(FTPPendingPhase.PRUNED),
        manifest_hash=manifest_hash,
        head="head-1",
        non_hybrid_plan_hash="3" * 64,
        previous_state_hash="4" * 64,
        current_ownership_hash="2" * 64,
    )
    validate_pending_resume(
        parsed.with_phase(FTPPendingPhase.OWNERSHIP_COMMITTED),
        manifest_hash="changed-local-view",
        head="new-head",
        current_ownership_hash="2" * 64,
    )

    with pytest.raises(PlanError, match="expected previous hash"):
        validate_pending_resume(
            parsed,
            manifest_hash=manifest_hash,
            head="head-1",
            non_hybrid_plan_hash="3" * 64,
            previous_state_hash="4" * 64,
            current_ownership_hash="2" * 64,
        )

    with pytest.raises(PlanError, match="Manifest"):
        validate_pending_resume(
            parsed,
            manifest_hash="3" * 64,
            head="head-1",
            non_hybrid_plan_hash="3" * 64,
            previous_state_hash="4" * 64,
            current_ownership_hash="1" * 64,
        )
    with pytest.raises(PlanError, match="HEAD"):
        validate_pending_resume(
            parsed,
            manifest_hash=manifest_hash,
            head="head-2",
            non_hybrid_plan_hash="3" * 64,
            previous_state_hash="4" * 64,
            current_ownership_hash="1" * 64,
        )
    with pytest.raises(PlanError, match="Ownership"):
        validate_pending_resume(
            parsed,
            manifest_hash=manifest_hash,
            head="head-1",
            non_hybrid_plan_hash="3" * 64,
            previous_state_hash="4" * 64,
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


def test_schema_one_pending_only_allows_post_commit_frozen_recovery(
    tmp_path: Path,
) -> None:
    """Legacy markers fail closed pre-commit but remain recoverable after Ownership."""

    target = _target()
    state = TargetState(1, target.name, target.fingerprint, "head-1", 10, {})
    legacy = FTPHybridPending(
        1,
        "github.com/acme/project",
        "frontend-root",
        ".",
        target.fingerprint,
        "deployment-1",
        FTPPendingPhase.PREPARED,
        "1" * 64,
        "2" * 64,
        local_manifest_hash(_manifest(tmp_path)),
        "head-1",
        state,
        11,
    )
    parsed = parse_pending(
        serialize_pending(legacy),
        project_id=legacy.project_id,
        mapping=legacy.mapping,
        remote=legacy.remote,
        target=target,
    )
    with pytest.raises(PlanError, match="schema 1 cannot safely resume"):
        validate_pending_resume(
            parsed,
            manifest_hash=legacy.local_manifest_hash,
            head=legacy.head,
            current_ownership_hash=legacy.previous_ownership_hash,
        )
    validate_pending_resume(
        parsed.with_phase(FTPPendingPhase.STATE_COMPLETE),
        current_ownership_hash=legacy.next_ownership_hash,
    )


def test_pending_manifest_hash_ignores_mirror_nested_state_keys(
    tmp_path: Path,
) -> None:
    """Schema 2 Pending Hash matches v1.7.3 even when State stores Mirror files.

    v1.7.3 put only Hybrid Root Files into ``plan.output_manifest``. Current
    Local State also records Mirror nested paths for incremental skip; those
    keys must not change Pending resume identity for the same aggregation tree.
    """

    local = _manifest(tmp_path)
    # v1.7.3 shape: incremental empty + hybrid root files only.
    v173_outputs = {item.name: item.entry for item in local.root_files}
    # Current Local State shape: root files + every Mirror nested file.
    full_state_outputs = hybrid_content_manifest(local)

    assert "assets/app.js" in full_state_outputs
    assert "assets/app.js" not in pending_manifest_outputs(local, full_state_outputs)
    assert pending_manifest_outputs(local, full_state_outputs) == v173_outputs

    expected = local_manifest_hash(local, v173_outputs)
    assert pending_local_manifest_hash(local, full_state_outputs) == expected
    assert pending_local_manifest_hash(local, v173_outputs) == expected
    # Unfiltered full State outputs would change the hash (the P1-01 regression).
    assert local_manifest_hash(local, full_state_outputs) != expected


def test_v173_style_pending_phases_resume_with_full_mirror_state(
    tmp_path: Path,
) -> None:
    """PREPARED / FILES_PUBLISHED / PRUNED markers keep matching after Mirror State."""

    target = _target()
    local = _manifest(tmp_path)
    full_state = hybrid_content_manifest(local)
    # Marker written with Schema 2 hash input shape (v1.7.3 / pending_local_*).
    manifest_hash = pending_local_manifest_hash(local, full_state)
    state = TargetState(
        1,
        target.name,
        target.fingerprint,
        "head-1",
        10,
        full_state,
    )
    base = FTPHybridPending(
        FTP_PENDING_SCHEMA,
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
        "3" * 64,
        "4" * 64,
    )
    # Current planner re-hashes with full State outputs filtered the same way.
    current_hash = pending_local_manifest_hash(local, full_state)
    for phase, ownership in (
        (FTPPendingPhase.PREPARED, "1" * 64),
        (FTPPendingPhase.FILES_PUBLISHED, "1" * 64),
        (FTPPendingPhase.PRUNED, "2" * 64),
    ):
        validate_pending_resume(
            base.with_phase(phase),
            manifest_hash=current_hash,
            head="head-1",
            non_hybrid_plan_hash="3" * 64,
            previous_state_hash="4" * 64,
            current_ownership_hash=ownership,
        )


def test_schema_two_pending_requires_both_contract_hashes(tmp_path: Path) -> None:
    """Strict Schema 2 parsing rejects a missing stable-plan or State identity."""

    target = _target()
    state = TargetState(1, target.name, target.fingerprint, "head-1", 10, {})
    record = FTPHybridPending(
        FTP_PENDING_SCHEMA,
        "github.com/acme/project",
        "frontend-root",
        ".",
        target.fingerprint,
        "deployment-1",
        FTPPendingPhase.PREPARED,
        "1" * 64,
        "2" * 64,
        local_manifest_hash(_manifest(tmp_path)),
        "head-1",
        state,
        11,
        "3" * 64,
        "4" * 64,
    )
    raw = json.loads(serialize_pending(record))
    del raw["previous_state_hash"]
    with pytest.raises(PlanError, match="invalid schema"):
        parse_pending(
            json.dumps(raw).encode(),
            project_id=record.project_id,
            mapping=record.mapping,
            remote=record.remote,
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
        self.encoding = "latin-1"

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

        if command == "FEAT":
            return "211-Features\n MLST type*;size*;modify*;\n UTF8\n211 End"
        assert command == "OPTS UTF8 ON"
        return "200 UTF8 enabled"

    def getwelcome(self) -> str:
        """Return one stable non-secret server greeting."""

        return "220 Fixture FTP"


def test_typed_mlsd_scanner_bounded_read_and_cache() -> None:
    """MLSD drives stable recursion, empty directories, FEAT, and bounded RETR."""

    transport = FTPTransport(_target())
    session = FakeMLSDSession()
    transport.ftp = cast(Any, session)

    assert transport.features() == frozenset({"MLST", "MLSD", "UTF8"})
    transport.enable_utf8()
    assert session.encoding == "utf-8"
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


def test_typed_mlsd_rejects_casefold_collisions_and_refreshes_cache() -> None:
    """Managed scans reject ambiguous names and explicit refresh sends a new MLSD."""

    transport = FTPTransport(_target())
    session = FakeMLSDSession()
    session.listings["/root"] = [
        ("App.js", {"type": "file", "size": "1"}),
        ("app.js", {"type": "file", "size": "1"}),
    ]
    transport.ftp = cast(Any, session)
    with pytest.raises(DeployError, match="colliding sibling names.*App.js.*app.js"):
        transport.list_directory_typed(".")

    session.listings["/root"] = [("assets", {"type": "dir"})]
    assert transport.lstat("assets") is RemotePathType.DIRECTORY
    session.listings["/root"] = [("assets", {"type": "file", "size": "0"})]
    assert transport.lstat("assets") is RemotePathType.DIRECTORY
    transport.refresh_remote_metadata()
    assert transport.lstat("assets") is RemotePathType.FILE


def test_case_insensitive_capability_probe_fails_closed() -> None:
    """A server that aliases case variants never produces a capability profile."""

    class CaseInsensitiveTransport:
        """Model only the probe operations needed to expose case aliasing."""

        def __init__(self) -> None:
            """Create case-folded file storage and explicit directory names."""

            self.files: dict[str, tuple[str, bytes]] = {}
            self.directories: set[str] = set()

        def features(self) -> frozenset[str]:
            """Advertise MLSD so path semantics become the decisive gate."""

            return frozenset({"MLSD", "UTF8"})

        def enable_utf8(self) -> None:
            """Accept UTF-8 so case aliasing remains the decisive failure."""

        def list_root_names(self) -> tuple[str, ...]:
            """Report no pre-existing root entries before the probe mutates."""

            return ()

        def make_directory(self, path: str, *, mode: int = 0o755) -> None:
            """Record one directory using case-insensitive identity."""

            del mode
            self.directories.add(path.casefold())

        def write_bytes(self, path: str, data: bytes) -> None:
            """Overwrite aliases exactly as a case-insensitive server would."""

            self.files[path.casefold()] = (PurePosixPath(path).name, data)

        def read_file(
            self,
            path: str,
            *,
            max_bytes: int,
            allow_case_collisions: bool = False,
        ) -> bytes:
            """Return the aliased file bytes within the requested bound."""

            del allow_case_collisions
            data = self.files[path.casefold()][1]
            assert len(data) <= max_bytes
            return data

        def list_directory_typed(
            self,
            path: str,
            *,
            allow_case_collisions: bool = False,
        ) -> tuple[FTPRemoteEntry, ...]:
            """Return direct children with the last spelling written."""

            del allow_case_collisions
            prefix = path.rstrip("/") + "/"
            names: dict[str, FTPRemoteEntry] = {}
            for directory in self.directories:
                if directory.startswith(prefix.casefold()):
                    relative = directory[len(prefix) :]
                    if relative and "/" not in relative:
                        names[relative] = FTPRemoteEntry(
                            relative, RemotePathType.DIRECTORY, None, None
                        )
            for key, (name, data) in self.files.items():
                if key.startswith(prefix.casefold()) and "/" not in key[len(prefix) :]:
                    names[name.casefold()] = FTPRemoteEntry(
                        name, RemotePathType.FILE, len(data), None
                    )
            return tuple(names.values())

        def remove_tree(
            self, path: str, *, allow_name_collisions: bool = False
        ) -> None:
            """Remove the current random probe root during finally cleanup."""

            del allow_name_collisions
            prefix = path.casefold().rstrip("/")
            self.files = {
                key: value
                for key, value in self.files.items()
                if not key.startswith(prefix)
            }
            self.directories = {
                value for value in self.directories if not value.startswith(prefix)
            }

        def remove_directory(
            self,
            path: str,
            *,
            allow_name_collisions: bool = False,
        ) -> None:
            """Best-effort-remove one empty shared directory."""

            del allow_name_collisions
            self.directories.discard(path.casefold())

    with pytest.raises(DeployError, match="case-insensitive"):
        probe_ftp_hybrid_capabilities(cast(Any, CaseInsensitiveTransport()), _target())
