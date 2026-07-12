"""Immutable expected-state schema, CAS-backed store, and generation pointer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .durable_io import durable_publish, ensure_state_directory, is_visible_state_file, list_orphan_temps
from .errors import ConfigurationError
from .target_identity import TargetIdentity

STATE_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 1
MANIFEST_LINEAGE_SCHEMA = 1


@dataclass(frozen=True)
class FileEntry:
    """One managed remote path recorded in an immutable state snapshot.

    Attributes:
        path: Remote path relative to remote_root.
        owner: Ownership domain (``source`` or artifact mapping id).
        content_sha256: Content digest, or ``None`` when the path is absent.
        executable: Whether the path is an executable regular file.
        exists: Whether the path is expected to exist remotely.
    """

    path: str
    owner: str
    content_sha256: str | None
    executable: bool = False
    exists: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return canonical JSON mapping.

        Returns:
            Sorted-key-friendly mapping.
        """

        return {
            "content_sha256": self.content_sha256,
            "executable": self.executable,
            "exists": self.exists,
            "owner": self.owner,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FileEntry:
        """Parse one file entry.

        Args:
            payload: JSON object for a single path.

        Returns:
            Typed file entry.
        """

        return cls(
            path=str(payload["path"]),
            owner=str(payload.get("owner", "source")),
            content_sha256=payload.get("content_sha256"),
            executable=bool(payload.get("executable", False)),
            exists=bool(payload.get("exists", True)),
        )


@dataclass(frozen=True)
class ExpectedState:
    """Immutable content-addressed expected remote state snapshot.

    Attributes:
        schema_version: Frozen schema major version.
        generation: Logical generation that produced this snapshot when current.
        parent_state_id: Previous state id, or ``None`` for generation 1.
        source_tree_id: Git tree object id for composed source.
        applied_transition_ids: Ordered unique first-parent transition ids.
        physical_fingerprint: Physical target identity fingerprint.
        policy_fingerprint: Managed-state policy fingerprint.
        files: Managed path entries.
        deployment_id: Deployment that created the snapshot, if any.
        artifacts: Artifact provenance records (opaque for Gate A).
    """

    schema_version: int
    generation: int
    parent_state_id: str | None
    source_tree_id: str
    applied_transition_ids: tuple[str, ...]
    physical_fingerprint: str
    policy_fingerprint: str
    files: tuple[FileEntry, ...]
    deployment_id: str | None = None
    artifacts: tuple[dict[str, Any], ...] = ()

    def canonical_dict(self) -> dict[str, Any]:
        """Return canonical JSON without the content-addressed state id.

        Returns:
            Mapping suitable for stable hashing.
        """

        return {
            "applied_transition_ids": list(self.applied_transition_ids),
            "artifacts": list(self.artifacts),
            "deployment_id": self.deployment_id,
            "files": [entry.to_dict() for entry in sorted(self.files, key=lambda item: item.path)],
            "generation": self.generation,
            "parent_state_id": self.parent_state_id,
            "physical_fingerprint": self.physical_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "schema_version": self.schema_version,
            "source_tree_id": self.source_tree_id,
        }

    def state_id(self) -> str:
        """Return content-addressed state identifier.

        Returns:
            ``sha256:`` prefixed digest of canonical JSON.
        """

        return f"sha256:{_sha256_canonical(self.canonical_dict())}"

    def to_dict(self) -> dict[str, Any]:
        """Return full serializable mapping including state_id.

        Returns:
            JSON-compatible mapping.
        """

        payload = self.canonical_dict()
        payload["state_id"] = self.state_id()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExpectedState:
        """Parse and validate an immutable state document.

        Args:
            payload: JSON object loaded from disk.

        Returns:
            Validated expected state.

        Raises:
            ConfigurationError: On unknown schema or hash mismatch.
        """

        schema = int(payload.get("schema_version", 0))
        if schema != STATE_SCHEMA_VERSION:
            raise ConfigurationError(f"unknown expected-state schema version: {schema}")
        files = tuple(FileEntry.from_dict(row) for row in payload.get("files", []))
        state = cls(
            schema_version=schema,
            generation=int(payload["generation"]),
            parent_state_id=payload.get("parent_state_id"),
            source_tree_id=str(payload["source_tree_id"]),
            applied_transition_ids=tuple(payload.get("applied_transition_ids", [])),
            physical_fingerprint=str(payload["physical_fingerprint"]),
            policy_fingerprint=str(payload["policy_fingerprint"]),
            files=files,
            deployment_id=payload.get("deployment_id"),
            artifacts=tuple(payload.get("artifacts", ())),
        )
        expected_id = payload.get("state_id")
        if expected_id is not None and expected_id != state.state_id():
            raise ConfigurationError(
                f"expected-state content hash mismatch: stored={expected_id} actual={state.state_id()}"
            )
        return state


@dataclass(frozen=True)
class CurrentPointer:
    """Mutable generation pointer referencing one immutable state.

    Attributes:
        schema_version: Pointer schema version.
        target_id: Physical target id owning the pointer.
        generation: Monotonic generation.
        state_id: Content-addressed current state id.
    """

    schema_version: int
    target_id: str
    generation: int
    state_id: str

    def to_dict(self) -> dict[str, Any]:
        """Return serializable pointer mapping.

        Returns:
            JSON-compatible mapping.
        """

        return {
            "generation": self.generation,
            "schema_version": self.schema_version,
            "state_id": self.state_id,
            "target_id": self.target_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CurrentPointer:
        """Parse a current pointer document.

        Args:
            payload: JSON object.

        Returns:
            Typed current pointer.
        """

        schema = int(payload.get("schema_version", 0))
        if schema != CURRENT_SCHEMA_VERSION:
            raise ConfigurationError(f"unknown current pointer schema version: {schema}")
        return cls(
            schema_version=schema,
            target_id=str(payload["target_id"]),
            generation=int(payload["generation"]),
            state_id=str(payload["state_id"]),
        )


@dataclass
class ManifestLineage:
    """Optional v0.2 state lineage fields attached to deployment manifests.

    Attributes:
        before_state_id: State id before the deployment.
        after_state_id: State id after the deployment.
        before_generation: Generation before CAS advance.
        after_generation: Generation after CAS advance.
        introduced_transition_ids: Transition ids introduced by this deployment.
        physical_fingerprint: Physical fingerprint at deploy time.
        policy_fingerprint: Policy fingerprint at deploy time.
        transaction_id: Owning transaction journal id.
        target_id: Physical target id.
        state: Lineage marker (``v1`` or ``legacy``).
    """

    before_state_id: str | None = None
    after_state_id: str | None = None
    before_generation: int | None = None
    after_generation: int | None = None
    introduced_transition_ids: list[str] = field(default_factory=list)
    physical_fingerprint: str | None = None
    policy_fingerprint: str | None = None
    transaction_id: str | None = None
    target_id: str | None = None
    state: str = "legacy"

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible lineage fields.

        Returns:
            Mapping of lineage keys.
        """

        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> ManifestLineage:
        """Parse lineage from a manifest payload.

        Args:
            payload: Optional lineage mapping or full manifest.

        Returns:
            Lineage with ``state=legacy`` when fields are absent.
        """

        if not payload:
            return cls(state="legacy")
        # Accept either nested lineage or flat v0.2 fields on the manifest root.
        nested = payload.get("lineage") if "lineage" in payload else payload
        if not isinstance(nested, dict):
            return cls(state="legacy")
        has_state = any(
            nested.get(key) is not None
            for key in (
                "before_state_id",
                "after_state_id",
                "before_generation",
                "after_generation",
                "transaction_id",
            )
        )
        if not has_state and nested.get("state") != "v1":
            return cls(state="legacy")
        return cls(
            before_state_id=nested.get("before_state_id"),
            after_state_id=nested.get("after_state_id"),
            before_generation=nested.get("before_generation"),
            after_generation=nested.get("after_generation"),
            introduced_transition_ids=list(nested.get("introduced_transition_ids", [])),
            physical_fingerprint=nested.get("physical_fingerprint"),
            policy_fingerprint=nested.get("policy_fingerprint"),
            transaction_id=nested.get("transaction_id"),
            target_id=nested.get("target_id"),
            state=str(nested.get("state", "v1")),
        )


class ExpectedStateStore:
    """Read/write immutable states and the durable current generation pointer."""

    def __init__(self, target_root: Path, identity: TargetIdentity | None = None):
        """Bind a target state root without creating files.

        Args:
            target_root: ``.../targets/<target-id>`` directory.
            identity: Optional identity used for pointer target_id checks.
        """

        self.root = target_root.resolve()
        self.identity = identity
        self.states_dir = self.root / "states"
        self.current_path = self.root / "current.json"

    def ensure_layout(self) -> None:
        """Create state directory layout with owner-only permissions.

        Returns:
            None.
        """

        ensure_state_directory(self.root)
        ensure_state_directory(self.states_dir)

    def write_state(self, state: ExpectedState) -> str:
        """Publish an immutable state file via durable atomic publisher.

        Args:
            state: Snapshot to persist.

        Returns:
            Content-addressed state id.
        """

        self.ensure_layout()
        state_id = state.state_id()
        path = self._state_path(state_id)
        if path.is_file():
            existing = self.read_state(state_id)
            if existing.canonical_dict() != state.canonical_dict():
                raise ConfigurationError(f"immutable state collision for {state_id}")
            return state_id
        payload = json.dumps(state.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        durable_publish(path, payload.encode("utf-8") + b"\n")
        return state_id

    def read_state(self, state_id: str) -> ExpectedState:
        """Load and re-hash one immutable state.

        Args:
            state_id: Content-addressed state identifier.

        Returns:
            Validated expected state.
        """

        path = self._state_path(state_id)
        if not path.is_file() or not is_visible_state_file(path):
            raise ConfigurationError(f"expected state not found: {state_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read expected state {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError(f"expected state root is not an object: {path}")
        state = ExpectedState.from_dict(payload)
        if state.state_id() != state_id and payload.get("state_id") != state_id:
            raise ConfigurationError(f"state id mismatch for {path}")
        return state

    def read_current(self) -> CurrentPointer | None:
        """Read the durable current pointer when present.

        Returns:
            Current pointer, or ``None`` when no current exists.
        """

        if not self.current_path.is_file():
            orphans = list_orphan_temps(self.root)
            if orphans:
                # Orphan temps must never be treated as current.
                pass
            return None
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read current pointer {self.current_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("current pointer root is not an object")
        pointer = CurrentPointer.from_dict(payload)
        return pointer

    def cas_advance(
        self,
        *,
        expected_generation: int | None,
        state: ExpectedState,
        target_id: str | None = None,
    ) -> CurrentPointer:
        """Write immutable state and CAS-advance current generation.

        Args:
            expected_generation: Generation observed before mutation; ``None`` for first.
            state: After-state snapshot (generation must equal expected+1 or 1).
            target_id: Explicit target id; defaults to identity.

        Returns:
            New current pointer.

        Raises:
            ConfigurationError: On generation conflict or missing prerequisites.
        """

        self.ensure_layout()
        current = self.read_current()
        if expected_generation is None:
            if current is not None:
                raise ConfigurationError(
                    f"current generation already exists ({current.generation}); cannot bootstrap"
                )
            if state.generation != 1:
                raise ConfigurationError("first current generation must be 1")
        else:
            if current is None:
                raise ConfigurationError("current pointer missing for CAS advance")
            if current.generation != expected_generation:
                raise ConfigurationError(
                    f"generation CAS conflict: expected {expected_generation}, actual {current.generation}"
                )
            if state.generation != expected_generation + 1:
                raise ConfigurationError(
                    f"after state generation must be {expected_generation + 1}, got {state.generation}"
                )
            # Old generation cannot rewrite current via smaller generation.
            if state.generation <= current.generation:
                raise ConfigurationError("refusing to move current pointer to older generation")

        state_id = self.write_state(state)
        resolved_target = target_id
        if resolved_target is None:
            if self.identity is None:
                raise ConfigurationError("target_id is required without bound identity")
            resolved_target = self.identity.target_id
        pointer = CurrentPointer(
            schema_version=CURRENT_SCHEMA_VERSION,
            target_id=resolved_target,
            generation=state.generation,
            state_id=state_id,
        )
        durable_publish(
            self.current_path,
            json.dumps(pointer.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            + b"\n",
        )
        return pointer

    def load_current_state(self) -> tuple[CurrentPointer, ExpectedState] | None:
        """Load current pointer and its immutable snapshot.

        Returns:
            Pointer and state, or ``None`` when no current exists.
        """

        pointer = self.read_current()
        if pointer is None:
            return None
        return pointer, self.read_state(pointer.state_id)

    def _state_path(self, state_id: str) -> Path:
        """Return the durable path for one state id.

        Args:
            state_id: Content-addressed identifier.

        Returns:
            Path under ``states/``.
        """

        safe = state_id.replace(":", "_")
        if "/" in safe or ".." in safe:
            raise ConfigurationError(f"invalid state id path: {state_id!r}")
        return self.states_dir / f"{safe}.json"


def build_expected_state(
    *,
    generation: int,
    parent_state_id: str | None,
    source_tree_id: str,
    applied_transition_ids: Sequence[str],
    physical_fingerprint: str,
    policy_fingerprint: str,
    files: Sequence[FileEntry] = (),
    deployment_id: str | None = None,
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> ExpectedState:
    """Construct an immutable expected state with schema v1.

    Args:
        generation: Snapshot generation number.
        parent_state_id: Previous state id.
        source_tree_id: Composed Git tree id.
        applied_transition_ids: Applied first-parent transition ids.
        physical_fingerprint: Physical target fingerprint.
        policy_fingerprint: Managed policy fingerprint.
        files: Managed file entries.
        deployment_id: Creating deployment id.
        artifacts: Artifact provenance records.

    Returns:
        Immutable expected state instance.
    """

    return ExpectedState(
        schema_version=STATE_SCHEMA_VERSION,
        generation=generation,
        parent_state_id=parent_state_id,
        source_tree_id=source_tree_id,
        applied_transition_ids=tuple(applied_transition_ids),
        physical_fingerprint=physical_fingerprint,
        policy_fingerprint=policy_fingerprint,
        files=tuple(files),
        deployment_id=deployment_id,
        artifacts=tuple(dict(item) for item in artifacts),
    )


def _sha256_canonical(payload: Mapping[str, Any]) -> str:
    """Hash canonical sorted JSON.

    Args:
        payload: JSON-compatible mapping.

    Returns:
        Hex digest.
    """

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
