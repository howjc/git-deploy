"""FTP In-place Hybrid capability, typed scan, and forward-resume records."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, cast

from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError, PlanError
from git_deploy.hybrid import HybridLocalManifest
from git_deploy.manifest import ManifestEntry, TargetState
from git_deploy.transports.base import RemotePathType
from git_deploy.transports.ftp import FTPTransport

FTP_HYBRID_SCHEMA = 1
FTP_PENDING_SCHEMA = 2
FTP_CAPABILITY_SCHEMA = 3
MAX_CAPABILITY_PROFILE_BYTES = 64 * 1024
MAX_PENDING_BYTES = 1024 * 1024
MAX_SCAN_DEPTH = 64
MAX_SCAN_ENTRIES = 200_000


@dataclass(frozen=True, slots=True)
class FTPHybridCapabilities:
    """Persist the FTP features proven by one explicit mutating probe."""

    schema: int
    target_fingerprint: str
    server_banner_hash: str
    case_sensitive_paths: bool
    mlsd: bool
    rename_cross_directory: bool
    rename_replace_file: bool
    retr: bool
    delete_file: bool
    remove_directory: bool
    probed_at: int
    utf8: bool = False
    unicode_paths: bool = False
    normalization_preserving: bool = False


class FTPPendingPhase(str, Enum):
    """Track the monotonic FTP In-place Hybrid execution lifecycle."""

    PREPARED = "PREPARED"
    FILES_PUBLISHED = "FILES_PUBLISHED"
    PRUNED = "PRUNED"
    OWNERSHIP_COMMITTED = "OWNERSHIP_COMMITTED"
    STATE_COMPLETE = "STATE_COMPLETE"


@dataclass(frozen=True, slots=True)
class FTPHybridPending:
    """Persist frozen deployment facts required for safe forward convergence."""

    schema: int
    project_id: str
    mapping: str
    remote: str
    target_fingerprint: str
    deployment_id: str
    phase: FTPPendingPhase
    previous_ownership_hash: str
    next_ownership_hash: str
    local_manifest_hash: str
    head: str
    next_state: TargetState
    created_at: int
    non_hybrid_plan_hash: str | None = None
    previous_state_hash: str | None = None

    def with_phase(self, phase: FTPPendingPhase) -> FTPHybridPending:
        """Return a copy advanced to one durable execution phase."""

        return replace(self, phase=phase)


@dataclass(frozen=True, slots=True)
class FTPRemoteTree:
    """Contain one typed recursive FTP directory snapshot."""

    root: str
    files: tuple[str, ...]
    directories: tuple[str, ...]


def capability_profile_path(runtime_base: Path, target: TargetConfig) -> Path:
    """Return the non-secret local profile path for one physical FTP target.

    Args:
        runtime_base: ``<git-common-dir>/git-deploy`` state directory.
        target: Resolved FTP target whose fingerprint names the cache entry.

    Returns:
        Stable JSON path below ``ftp-capabilities``.
    """

    return _capability_profile_path(runtime_base, target.fingerprint)


def serialize_capabilities(profile: FTPHybridCapabilities) -> bytes:
    """Serialize one capability profile as deterministic UTF-8 JSON."""

    payload = {
        "schema": profile.schema,
        "target_fingerprint": profile.target_fingerprint,
        "server_banner_hash": profile.server_banner_hash,
        "features": {
            "mlsd": profile.mlsd,
            "case_sensitive_paths": profile.case_sensitive_paths,
            "retr": profile.retr,
            "rename_cross_directory": profile.rename_cross_directory,
            "rename_replace_file": profile.rename_replace_file,
            "delete_file": profile.delete_file,
            "remove_directory": profile.remove_directory,
            "utf8": profile.utf8,
            "unicode_paths": profile.unicode_paths,
            "normalization_preserving": profile.normalization_preserving,
        },
        "probed_at": profile.probed_at,
    }
    return _json_bytes(payload)


def parse_capabilities(data: bytes) -> FTPHybridCapabilities:
    """Validate untrusted local capability JSON and return its typed record."""

    if len(data) > MAX_CAPABILITY_PROFILE_BYTES:
        raise PlanError("FTP Hybrid Capability Profile exceeds its size limit")
    raw = _parse_json_object(data, "FTP Hybrid Capability Profile")
    features = raw.get("features")
    expected_features = {
        "mlsd",
        "case_sensitive_paths",
        "retr",
        "rename_cross_directory",
        "rename_replace_file",
        "delete_file",
        "remove_directory",
        "utf8",
        "unicode_paths",
        "normalization_preserving",
    }
    if (
        set(raw)
        != {
            "schema",
            "target_fingerprint",
            "server_banner_hash",
            "features",
            "probed_at",
        }
        or raw.get("schema") != FTP_CAPABILITY_SCHEMA
        or not isinstance(features, dict)
        or set(features) != expected_features
        or not all(isinstance(features[name], bool) for name in expected_features)
    ):
        if raw.get("schema") in {1, 2}:
            raise PlanError(
                "FTP Hybrid Capability Profile is obsolete and does not prove UTF-8 "
                "normalization-preserving path semantics; "
                "run Doctor --probe-ftp-hybrid again"
            )
        raise PlanError("FTP Hybrid Capability Profile has an invalid schema")
    fingerprint = raw.get("target_fingerprint")
    banner_hash = raw.get("server_banner_hash")
    probed_at = raw.get("probed_at")
    if (
        not isinstance(fingerprint, str)
        or not fingerprint
        or not _valid_sha256(banner_hash)
        or not isinstance(probed_at, int)
        or isinstance(probed_at, bool)
        or probed_at < 0
    ):
        raise PlanError("FTP Hybrid Capability Profile contains invalid identity fields")
    return FTPHybridCapabilities(
        FTP_CAPABILITY_SCHEMA,
        fingerprint,
        cast(str, banner_hash),
        features["case_sensitive_paths"],
        features["mlsd"],
        features["rename_cross_directory"],
        features["rename_replace_file"],
        features["retr"],
        features["delete_file"],
        features["remove_directory"],
        probed_at,
        features["utf8"],
        features["unicode_paths"],
        features["normalization_preserving"],
    )


def save_capability_profile(runtime_base: Path, profile: FTPHybridCapabilities) -> Path:
    """Atomically persist one successful non-secret FTP capability proof.

    Args:
        runtime_base: ``<git-common-dir>/git-deploy`` state directory.
        profile: Fully successful probe result.

    Returns:
        Final profile path.
    """

    path = _capability_profile_path(runtime_base, profile.target_fingerprint)
    _atomic_write(path, serialize_capabilities(profile))
    return path


def load_capability_profile(
    runtime_base: Path,
    target: TargetConfig,
    *,
    server_banner_hash: str,
) -> FTPHybridCapabilities:
    """Load a profile bound to the exact target and current server banner.

    Args:
        runtime_base: ``<git-common-dir>/git-deploy`` state directory.
        target: Connected resolved FTP target.
        server_banner_hash: Current non-secret banner hash.

    Returns:
        Validated all-required-capabilities profile.
    """

    path = capability_profile_path(runtime_base, target)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise PlanError(
            "FTP Hybrid Capability Profile is missing; run "
            f"git-deploy doctor {target.name} --probe-ftp-hybrid"
        ) from exc
    except OSError as exc:
        raise PlanError(f"cannot read FTP Hybrid Capability Profile {path}: {exc}") from exc
    profile = parse_capabilities(data)
    if profile.target_fingerprint != target.fingerprint:
        raise PlanError("FTP Hybrid Capability Profile target changed; run Doctor probe again")
    if profile.server_banner_hash != server_banner_hash:
        raise PlanError("FTP server banner changed; run Doctor --probe-ftp-hybrid again")
    if not all(
        (
            profile.mlsd,
            profile.case_sensitive_paths,
            profile.rename_cross_directory,
            profile.rename_replace_file,
            profile.retr,
            profile.delete_file,
            profile.remove_directory,
            profile.utf8,
            profile.unicode_paths,
            profile.normalization_preserving,
        )
    ):
        raise PlanError(
            "FTP Hybrid Capability Profile does not satisfy required capabilities"
        )
    return profile


def probe_ftp_hybrid_capabilities(
    transport: FTPTransport,
    target: TargetConfig,
    *,
    now: int | None = None,
) -> FTPHybridCapabilities:
    """Mutate only the protected probe root and prove the FTP Hybrid contract.

    Args:
        transport: Connected FTP transport for the explicitly confirmed target.
        target: Resolved target identity bound to the result.
        now: Optional deterministic timestamp for tests.

    Returns:
        All-true capability profile after successful cleanup.
    """

    if target.protocol != "ftp":
        raise PlanError("FTP Hybrid capability probing requires an FTP target")
    features = transport.features()
    if "MLSD" not in features:
        raise DeployError("FTP server does not advertise mandatory MLSD support")
    if "UTF8" not in features:
        raise DeployError("FTP server does not advertise mandatory UTF8 support")
    transport.enable_utf8()
    probe_root = f".git-deploy/ftp-probe/{secrets.token_hex(16)}"
    primary_error: BaseException | None = None
    try:
        transport.make_directory(f"{probe_root}/a")
        transport.make_directory(f"{probe_root}/b")
        transport.make_directory(f"{probe_root}/empty")
        binary = b"\x00git-deploy\xff\r\n" + secrets.token_bytes(64)
        transport.write_bytes(f"{probe_root}/a/binary.bin", binary)
        transport.write_bytes(f"{probe_root}/a/zero.bin", b"")
        if transport.read_file(f"{probe_root}/a/binary.bin", max_bytes=len(binary)) != binary:
            raise DeployError("FTP binary round-trip hash mismatch")
        if transport.read_file(f"{probe_root}/a/zero.bin", max_bytes=0) != b"":
            raise DeployError("FTP zero-byte round-trip mismatch")
        typed = {item.path: item.kind for item in transport.list_directory_typed(probe_root)}
        if typed.get("a") is not RemotePathType.DIRECTORY:
            raise DeployError("FTP MLSD did not classify the probe directory")
        if transport.list_directory_typed(f"{probe_root}/empty"):
            raise DeployError("FTP MLSD did not report an empty probe directory")

        case_root = f"{probe_root}/case"
        upper_case = f"{case_root}/CaseProbe.bin"
        lower_case = f"{case_root}/caseprobe.bin"
        renamed_case = f"{case_root}/CASEPROBE.bin"
        transport.make_directory(case_root)
        transport.write_bytes(upper_case, b"upper-case-path")
        transport.write_bytes(lower_case, b"lower-case-path")
        case_entries = transport.list_directory_typed(
            case_root,
            allow_case_collisions=True,
        )
        case_names = tuple(item.path for item in case_entries)
        if len(case_names) != 2 or set(case_names) != {"CaseProbe.bin", "caseprobe.bin"}:
            raise DeployError(
                "FTP Hybrid unsupported: remote filesystem is case-insensitive"
            )
        if transport.read_file(
            upper_case,
            max_bytes=15,
            allow_case_collisions=True,
        ) != b"upper-case-path":
            raise DeployError("FTP case-sensitive path probe aliased the uppercase file")
        if transport.read_file(
            lower_case,
            max_bytes=15,
            allow_case_collisions=True,
        ) != b"lower-case-path":
            raise DeployError("FTP case-sensitive path probe aliased the lowercase file")
        transport.delete_typed(upper_case, allow_case_collisions=True)
        if transport.read_file(lower_case, max_bytes=15) != b"lower-case-path":
            raise DeployError("FTP deleting one case variant affected the other file")
        transport.rename_replace(lower_case, renamed_case)
        if transport.lstat(lower_case) is not RemotePathType.MISSING:
            raise DeployError("FTP case-only rename left the source name behind")
        if transport.read_file(renamed_case, max_bytes=15) != b"lower-case-path":
            raise DeployError("FTP case-only rename did not preserve content")

        unicode_root = f"{probe_root}/unicode"
        chinese_name = "部署-文件.txt"
        renamed_chinese = "已发布-文件.txt"
        nfc_name = "caf\u00e9.txt"
        nfd_name = unicodedata.normalize("NFD", nfc_name)
        transport.make_directory(unicode_root)
        transport.write_bytes(f"{unicode_root}/{chinese_name}", b"chinese-name")
        transport.write_bytes(f"{unicode_root}/{nfc_name}", b"nfc-name")
        transport.write_bytes(f"{unicode_root}/{nfd_name}", b"nfd-name")
        unicode_entries = transport.list_directory_typed(
            unicode_root,
            allow_case_collisions=True,
        )
        unicode_names = {item.path for item in unicode_entries}
        if unicode_names != {chinese_name, nfc_name, nfd_name}:
            raise DeployError(
                "FTP Hybrid unsupported: MLSD did not preserve exact UTF-8 NFC/NFD names"
            )
        if (
            transport.read_file(
                f"{unicode_root}/{nfc_name}",
                max_bytes=8,
                allow_case_collisions=True,
            )
            != b"nfc-name"
        ):
            raise DeployError("FTP NFC path probe returned the wrong file")
        if (
            transport.read_file(
                f"{unicode_root}/{nfd_name}",
                max_bytes=8,
                allow_case_collisions=True,
            )
            != b"nfd-name"
        ):
            raise DeployError("FTP NFD path probe returned the wrong file")
        transport.delete_typed(
            f"{unicode_root}/{nfd_name}",
            allow_case_collisions=True,
        )
        if (
            transport.read_file(f"{unicode_root}/{nfc_name}", max_bytes=8)
            != b"nfc-name"
        ):
            raise DeployError("FTP deleting NFD path affected the NFC path")
        transport.rename_replace(
            f"{unicode_root}/{chinese_name}",
            f"{unicode_root}/{renamed_chinese}",
        )
        if (
            transport.lstat(f"{unicode_root}/{chinese_name}")
            is not RemotePathType.MISSING
        ):
            raise DeployError("FTP Unicode rename left the source name behind")
        if (
            transport.read_file(
                f"{unicode_root}/{renamed_chinese}",
                max_bytes=12,
            )
            != b"chinese-name"
        ):
            raise DeployError("FTP Unicode rename did not preserve content")

        cross_source = f"{probe_root}/a/cross.bin"
        cross_final = f"{probe_root}/b/cross.bin"
        transport.write_bytes(cross_source, b"cross-directory")
        transport.rename_replace(cross_source, cross_final)
        if transport.read_file(cross_final, max_bytes=64) != b"cross-directory":
            raise DeployError("FTP cross-directory rename did not preserve content")

        replace_source = f"{probe_root}/a/replacement.bin"
        replace_final = f"{probe_root}/b/existing.bin"
        transport.write_bytes(replace_source, b"new")
        transport.write_bytes(replace_final, b"old")
        transport.rename_replace(replace_source, replace_final)
        if transport.read_file(replace_final, max_bytes=16) != b"new":
            raise DeployError("FTP rename replace did not publish source content")
        if transport.lstat(replace_source) is not RemotePathType.MISSING:
            raise DeployError("FTP rename replace left the source behind")
        transport.delete_typed(replace_final)
        transport.remove_directory(f"{probe_root}/empty")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            transport.remove_tree(probe_root, allow_name_collisions=True)
        except BaseException as cleanup_error:
            if primary_error is None:
                raise DeployError(
                    f"FTP capability probe cleanup failed: {cleanup_error}"
                ) from cleanup_error
            primary_error.add_note(
                "FTP capability probe also failed to clean its protected temporary root: "
                f"{cleanup_error}"
            )
        try:
            transport.remove_directory(".git-deploy/ftp-probe")
        except BaseException:
            # Sibling probes are protected state owned by another/older run. The
            # shared parent is cosmetic and must never invalidate this probe.
            pass
    return FTPHybridCapabilities(
        FTP_CAPABILITY_SCHEMA,
        target.fingerprint,
        transport.server_banner_hash(),
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        int(time.time()) if now is None else now,
        True,
        True,
        True,
    )


def local_manifest_hash(
    manifest: HybridLocalManifest,
    outputs: dict[str, ManifestEntry] | None = None,
) -> str:
    """Return a canonical hash for Hybrid plus all incremental output content."""

    payload = {
        "mapping": manifest.mapping,
        "remote": manifest.remote,
        "root_files": {
            item.name: asdict(item.entry) for item in manifest.root_files
        },
        "directories": {
            item.name: {
                "files": {
                    path: asdict(scanned.entry) for path, scanned in item.files.items()
                },
                "directories": list(item.directories),
            }
            for item in manifest.directories
        },
        "outputs": {
            path: asdict(entry) for path, entry in sorted((outputs or {}).items())
        },
    }
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def pending_path(mapping: str) -> str:
    """Return the protected remote marker path for a validated mapping."""

    if not mapping or mapping in {".", ".."} or "/" in mapping or "\\" in mapping:
        raise PlanError(f"unsafe FTP Hybrid mapping name: {mapping!r}")
    return f".git-deploy/ftp-hybrid/pending/{mapping}.json"


def serialize_pending(record: FTPHybridPending) -> bytes:
    """Serialize a Forward Resume marker as deterministic UTF-8 JSON."""

    state = record.next_state
    payload = {
        "schema": record.schema,
        "project_id": record.project_id,
        "mapping": record.mapping,
        "remote": record.remote,
        "target_fingerprint": record.target_fingerprint,
        "deployment_id": record.deployment_id,
        "phase": record.phase.value,
        "previous_ownership_hash": record.previous_ownership_hash,
        "next_ownership_hash": record.next_ownership_hash,
        "local_manifest_hash": record.local_manifest_hash,
        "head": record.head,
        "next_state": {
            "schema": state.schema,
            "target": state.target,
            "target_fingerprint": state.target_fingerprint,
            "last_commit": state.last_commit,
            "deployed_at": state.deployed_at,
            "outputs": {
                path: asdict(entry) for path, entry in sorted(state.outputs.items())
            },
        },
        "created_at": record.created_at,
    }
    if record.schema >= FTP_PENDING_SCHEMA:
        payload["non_hybrid_plan_hash"] = record.non_hybrid_plan_hash
        payload["previous_state_hash"] = record.previous_state_hash
    data = _json_bytes(payload)
    if len(data) > MAX_PENDING_BYTES:
        raise PlanError("FTP Hybrid Pending Marker exceeds its size limit")
    return data


def read_pending(
    transport: FTPTransport,
    *,
    project_id: str,
    mapping: str,
    remote: str,
    target: TargetConfig,
) -> FTPHybridPending | None:
    """Read one bounded typed Forward Resume marker from the FTP target.

    Args:
        transport: Connected FTP adapter.
        project_id: Expected credential-free project identity.
        mapping: Expected Hybrid mapping.
        remote: Expected mapping destination.
        target: Exact target identity.

    Returns:
        Valid marker, or ``None`` when its exact path is absent.
    """

    path = pending_path(mapping)
    kind = transport.lstat(path)
    if kind is RemotePathType.MISSING:
        return None
    if kind is not RemotePathType.FILE:
        raise PlanError(f"FTP Hybrid Pending path is not a regular file: {path}")
    return parse_pending(
        transport.read_file(path, max_bytes=MAX_PENDING_BYTES),
        project_id=project_id,
        mapping=mapping,
        remote=remote,
        target=target,
    )


def publish_verified_bytes(
    transport: FTPTransport,
    *,
    stage_path: str,
    final_path: str,
    data: bytes,
) -> None:
    """STOR, RETR-verify, rename-replace, and re-verify one small record.

    Args:
        transport: Connected FTP adapter with a valid Capability Profile.
        stage_path: Unique protected staging path.
        final_path: Protected final metadata path.
        data: Complete bytes to publish.

    Returns:
        ``None`` only after both staged and final SHA256 checks succeed.
    """

    expected = hashlib.sha256(data).digest()
    transport.write_bytes(stage_path, data)
    staged = transport.read_file(stage_path, max_bytes=len(data))
    if hashlib.sha256(staged).digest() != expected:
        raise DeployError(f"FTP staged verification failed for {stage_path}")
    transport.rename_replace(stage_path, final_path)
    final = transport.read_file(final_path, max_bytes=len(data))
    if hashlib.sha256(final).digest() != expected:
        raise DeployError(f"FTP final verification failed for {final_path}")
    if transport.lstat(stage_path) is not RemotePathType.MISSING:
        raise DeployError(f"FTP staged file was not consumed: {stage_path}")


def parse_pending(
    data: bytes,
    *,
    project_id: str,
    mapping: str,
    remote: str,
    target: TargetConfig,
) -> FTPHybridPending:
    """Validate a remote Pending Marker against exact project and target identity."""

    if len(data) > MAX_PENDING_BYTES:
        raise PlanError("FTP Hybrid Pending Marker exceeds its size limit")
    raw = _parse_json_object(data, "FTP Hybrid Pending Marker")
    legacy_required = {
        "schema",
        "project_id",
        "mapping",
        "remote",
        "target_fingerprint",
        "deployment_id",
        "phase",
        "previous_ownership_hash",
        "next_ownership_hash",
        "local_manifest_hash",
        "head",
        "next_state",
        "created_at",
    }
    schema = raw.get("schema")
    required = (
        legacy_required
        if schema == FTP_HYBRID_SCHEMA
        else legacy_required | {"non_hybrid_plan_hash", "previous_state_hash"}
    )
    if schema not in {FTP_HYBRID_SCHEMA, FTP_PENDING_SCHEMA} or set(raw) != required:
        raise PlanError("FTP Hybrid Pending Marker has an invalid schema")
    if (
        raw.get("project_id") != project_id
        or raw.get("mapping") != mapping
        or raw.get("remote") != remote
        or raw.get("target_fingerprint") != target.fingerprint
    ):
        raise PlanError("FTP Hybrid Pending Marker identity does not match this deployment")
    try:
        phase = FTPPendingPhase(raw.get("phase"))
    except (TypeError, ValueError) as exc:
        raise PlanError("FTP Hybrid Pending Marker has an unknown phase") from exc
    deployment_id = raw.get("deployment_id")
    head = raw.get("head")
    created_at = raw.get("created_at")
    hashes = (
        raw.get("previous_ownership_hash"),
        raw.get("next_ownership_hash"),
        raw.get("local_manifest_hash"),
    )
    contract_hashes = (
        raw.get("non_hybrid_plan_hash"),
        raw.get("previous_state_hash"),
    )
    if (
        not isinstance(deployment_id, str)
        or not deployment_id
        or not isinstance(head, str)
        or not head
        or not all(_valid_sha256(value) for value in hashes)
        or not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or created_at < 0
        or (
            schema == FTP_PENDING_SCHEMA
            and not all(_valid_sha256(value) for value in contract_hashes)
        )
    ):
        raise PlanError("FTP Hybrid Pending Marker contains invalid deployment fields")
    next_state = _parse_pending_state(raw.get("next_state"), target)
    if next_state.last_commit != head:
        raise PlanError(
            "FTP Hybrid Pending Marker HEAD does not match its frozen State"
        )
    return FTPHybridPending(
        cast(int, schema),
        project_id,
        mapping,
        remote,
        target.fingerprint,
        deployment_id,
        phase,
        cast(str, hashes[0]),
        cast(str, hashes[1]),
        cast(str, hashes[2]),
        head,
        next_state,
        created_at,
        cast(str, contract_hashes[0]) if schema == FTP_PENDING_SCHEMA else None,
        cast(str, contract_hashes[1]) if schema == FTP_PENDING_SCHEMA else None,
    )


def validate_pending_resume(
    pending: FTPHybridPending,
    *,
    manifest_hash: str | None = None,
    head: str | None = None,
    non_hybrid_plan_hash: str | None = None,
    previous_state_hash: str | None = None,
    current_ownership_hash: str,
) -> None:
    """Validate phase-sensitive local facts and the exact Ownership hash matrix."""

    local_required = pending.phase in {
        FTPPendingPhase.PREPARED,
        FTPPendingPhase.FILES_PUBLISHED,
        FTPPendingPhase.PRUNED,
    }
    if local_required:
        if pending.schema == FTP_HYBRID_SCHEMA:
            raise PlanError(
                "FTP Hybrid Pending schema 1 cannot safely resume before Ownership "
                "commit; restore git-deploy v1.5.1 for inspection or remove the marker "
                "only after a manual remote-state review"
            )
        if pending.local_manifest_hash != manifest_hash:
            raise PlanError(
                "Pending Manifest does not match current local deployment view"
            )
        if pending.head != head:
            raise PlanError(
                "Pending HEAD does not match the current local deployment view"
            )
        if pending.non_hybrid_plan_hash != non_hybrid_plan_hash:
            raise PlanError(
                "Pending non-Hybrid plan does not match the current deployment"
            )
        if pending.previous_state_hash != previous_state_hash:
            raise PlanError(
                "Pending previous State does not match the current deployment"
            )
    validate_pending_ownership_phase(pending, current_ownership_hash)


def validate_pending_ownership_phase(
    pending: FTPHybridPending,
    current_ownership_hash: str,
) -> None:
    """Require the exact Ownership hash class permitted by one Pending phase.

    Args:
        pending: Validated Forward Resume marker.
        current_ownership_hash: Hash of the current remote Ownership bytes.

    Returns:
        ``None`` when the state-machine phase and Ownership agree.
    """

    expected_hashes = {
        FTPPendingPhase.PREPARED: {pending.previous_ownership_hash},
        FTPPendingPhase.FILES_PUBLISHED: {pending.previous_ownership_hash},
        FTPPendingPhase.PRUNED: {
            pending.previous_ownership_hash,
            pending.next_ownership_hash,
        },
        FTPPendingPhase.OWNERSHIP_COMMITTED: {pending.next_ownership_hash},
        FTPPendingPhase.STATE_COMPLETE: {pending.next_ownership_hash},
    }[pending.phase]
    if current_ownership_hash not in expected_hashes:
        expected = "previous or next" if len(expected_hashes) == 2 else (
            "next" if pending.next_ownership_hash in expected_hashes else "previous"
        )
        raise PlanError(
            "FTP Hybrid Pending Ownership is inconsistent with phase "
            f"{pending.phase.value}: expected {expected} hash; manual inspection required"
        )


def scan_ftp_tree(
    transport: FTPTransport,
    root: str,
    *,
    max_depth: int = MAX_SCAN_DEPTH,
    max_entries: int = MAX_SCAN_ENTRIES,
) -> FTPRemoteTree:
    """Recursively scan one known managed directory using strict MLSD types.

    Args:
        transport: Connected FTP adapter.
        root: Relative managed directory below the target root.
        max_depth: Maximum recursive child depth.
        max_entries: Maximum combined file and directory entries.

    Returns:
        Stable relative file and directory paths, including empty directories.
    """

    if max_depth < 0 or max_entries < 0:
        raise PlanError("FTP Hybrid scan limits must be non-negative")
    kind = transport.lstat(root)
    if kind is RemotePathType.MISSING:
        return FTPRemoteTree(root, (), ())
    if kind is not RemotePathType.DIRECTORY:
        raise PlanError(f"FTP Hybrid scan root is not a directory: {root}")
    files: list[str] = []
    directories: list[str] = []
    pending: list[tuple[str, str, int]] = [(root, "", 0)]
    while pending:
        absolute, relative_root, depth = pending.pop()
        for entry in transport.list_directory_typed(absolute):
            relative = entry.path if not relative_root else f"{relative_root}/{entry.path}"
            if len(files) + len(directories) >= max_entries:
                raise PlanError(f"FTP Hybrid scan exceeds {max_entries} entries")
            if entry.kind is RemotePathType.FILE:
                files.append(relative)
                continue
            if entry.kind is not RemotePathType.DIRECTORY:
                raise PlanError(f"FTP Hybrid scan found unsupported type: {relative}")
            if depth + 1 > max_depth:
                raise PlanError(f"FTP Hybrid scan exceeds depth {max_depth}: {relative}")
            directories.append(relative)
            pending.append((f"{absolute}/{entry.path}", relative, depth + 1))
    return FTPRemoteTree(root, tuple(sorted(files)), tuple(sorted(directories)))


def _parse_pending_state(value: Any, target: TargetConfig) -> TargetState:
    """Validate the frozen State nested inside a remote Pending Marker."""

    if not isinstance(value, dict) or set(value) != {
        "schema",
        "target",
        "target_fingerprint",
        "last_commit",
        "deployed_at",
        "outputs",
    }:
        raise PlanError("FTP Hybrid Pending Marker contains an invalid next_state")
    if (
        value.get("schema") != 1
        or value.get("target") != target.name
        or value.get("target_fingerprint") != target.fingerprint
        or not isinstance(value.get("last_commit"), str)
        or not value.get("last_commit")
        or not isinstance(value.get("deployed_at"), int)
        or isinstance(value.get("deployed_at"), bool)
        or value.get("deployed_at") < 0
        or not isinstance(value.get("outputs"), dict)
    ):
        raise PlanError("FTP Hybrid Pending Marker next_state identity is invalid")
    outputs: dict[str, ManifestEntry] = {}
    for path, raw_entry in value["outputs"].items():
        candidate = PurePosixPath(path) if isinstance(path, str) else PurePosixPath("/")
        if (
            not isinstance(path, str)
            or not path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or not isinstance(raw_entry, dict)
            or set(raw_entry) != {"sha256", "size"}
            or not _valid_sha256(raw_entry.get("sha256"))
            or not isinstance(raw_entry.get("size"), int)
            or isinstance(raw_entry.get("size"), bool)
            or raw_entry.get("size") < 0
        ):
            raise PlanError("FTP Hybrid Pending Marker next_state output is invalid")
        outputs[path] = ManifestEntry(raw_entry["sha256"], raw_entry["size"])
    return TargetState(
        1,
        target.name,
        target.fingerprint,
        value["last_commit"],
        value["deployed_at"],
        outputs,
    )


def _capability_profile_path(runtime_base: Path, fingerprint: str) -> Path:
    """Hash one non-empty target fingerprint into its local cache filename."""

    if not fingerprint:
        raise PlanError("FTP Hybrid Capability Profile has an empty target fingerprint")
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return runtime_base / "ftp-capabilities" / f"{digest}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically replace one local file and fsync its containing directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise PlanError(f"cannot atomically write FTP Hybrid Capability Profile {path}: {exc}") from exc


def _parse_json_object(data: bytes, label: str) -> dict[str, Any]:
    """Decode strict UTF-8 JSON and require an object root."""

    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise PlanError(f"{label} must contain a JSON object")
    return raw


def _json_bytes(value: Any) -> bytes:
    """Return deterministic compact UTF-8 JSON bytes."""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _valid_sha256(value: object) -> bool:
    """Return whether a value is one lowercase SHA256 digest."""

    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "FTP_HYBRID_SCHEMA",
    "FTP_PENDING_SCHEMA",
    "FTP_CAPABILITY_SCHEMA",
    "MAX_CAPABILITY_PROFILE_BYTES",
    "MAX_PENDING_BYTES",
    "MAX_SCAN_DEPTH",
    "MAX_SCAN_ENTRIES",
    "FTPHybridCapabilities",
    "FTPHybridPending",
    "FTPPendingPhase",
    "FTPRemoteTree",
    "capability_profile_path",
    "load_capability_profile",
    "local_manifest_hash",
    "parse_capabilities",
    "parse_pending",
    "pending_path",
    "publish_verified_bytes",
    "probe_ftp_hybrid_capabilities",
    "read_pending",
    "save_capability_profile",
    "scan_ftp_tree",
    "serialize_capabilities",
    "serialize_pending",
    "validate_pending_resume",
    "validate_pending_ownership_phase",
]
