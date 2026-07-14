"""Trusted current/target artifact planning and first-baseline verification."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from .build_cache import BuildCacheEntry
from .errors import PolicyError
from .expected_state import ExpectedState, FileEntry
from .gitrepo import assert_managed_path_allowed
from .models import PlannedFile, ProjectConfig
from .remote_verify import remote_path_for


class ArtifactReadTransport(Protocol):
    """Read surface required for a known-source artifact baseline."""

    def read_file(self, remote_path: str) -> bytes | None:
        """Read one remote regular file or return ``None``."""


class ArtifactBaselineTransport(ArtifactReadTransport, Protocol):
    """Read/list surface required to prove an empty artifact baseline."""

    def list_files(self, remote_prefix: str) -> tuple[str, ...]:
        """List regular files at/below a remote prefix."""


@dataclass(frozen=True)
class ArtifactPlan:
    """Artifact difference or an explicit first-baseline requirement."""

    status: str
    files: tuple[PlannedFile, ...]
    target_entries: tuple[FileEntry, ...]
    content_refs: tuple[tuple[str, str], ...]
    reason: str | None = None


@dataclass(frozen=True)
class ArtifactBaseline:
    """Verified current artifact entries and provenance for generation state."""

    files: tuple[FileEntry, ...]
    provenance: tuple[dict[str, Any], ...]


class ArtifactPlanner:
    """Plan artifacts only from trusted immutable state and cached target manifests."""

    def plan(
        self,
        project: ProjectConfig,
        current: ExpectedState,
        target: BuildCacheEntry,
    ) -> ArtifactPlan:
        """Compare trusted current artifact entries with target cached artifacts."""

        if project.artifacts and not current.artifacts:
            return ArtifactPlan(
                status="baseline_required",
                files=(),
                target_entries=(),
                content_refs=(),
                reason="current state has no trusted artifact manifest",
            )
        current_entries = {
            item.path: item
            for item in current.files
            if item.owner.startswith("artifact:")
        }
        target_entries = {
            item.destination: FileEntry(
                path=item.destination,
                owner=item.owner,
                content_sha256=item.content_sha256,
                executable=item.executable,
                exists=True,
            )
            for item in target.artifacts
        }
        target_sizes = {item.destination: item.size for item in target.artifacts}
        for path in set(current_entries) | set(target_entries):
            # Artifacts share the same remote namespace and protected policy as source.
            assert_managed_path_allowed(project, path)
        operations: list[PlannedFile] = []
        for path in sorted(set(current_entries) | set(target_entries)):
            before = current_entries.get(path)
            after = target_entries.get(path)
            if after is None:
                operations.append(
                    PlannedFile(
                        action="delete",
                        path=path,
                        remote_path=remote_path_for(project, path),
                        source_path=None,
                        expected_before_sha256=before.content_sha256 if before else None,
                        target_sha256=None,
                        expected_before_executable=before.executable if before else None,
                    )
                )
                continue
            if (
                before is not None
                and before.content_sha256 == after.content_sha256
                and before.executable == after.executable
                and before.exists
            ):
                continue
            operations.append(
                PlannedFile(
                    action="upload",
                    path=path,
                    remote_path=remote_path_for(project, path),
                    source_path=path,
                    expected_before_sha256=(
                        before.content_sha256 if before and before.exists else None
                    ),
                    target_sha256=after.content_sha256,
                    target_size=target_sizes[path],
                    executable=after.executable,
                    expected_before_executable=(
                        before.executable if before and before.exists else None
                    ),
                )
            )
        return ArtifactPlan(
            status="ready",
            files=tuple(operations),
            target_entries=tuple(target_entries[path] for path in sorted(target_entries)),
            content_refs=tuple(
                (item.destination, item.content_sha256)
                for item in sorted(target.artifacts, key=lambda row: row.destination)
            ),
        )

    def verify_known_source_baseline(
        self,
        project: ProjectConfig,
        baseline: BuildCacheEntry,
        transport: ArtifactReadTransport,
    ) -> ArtifactBaseline:
        """Adopt only remote bytes exactly reproduced from a known source tree."""

        writes_before = getattr(transport, "write_calls", 0)
        entries: list[FileEntry] = []
        for item in baseline.artifacts:
            remote = remote_path_for(project, item.destination)
            actual = transport.read_file(remote)
            actual_hash = hashlib.sha256(actual).hexdigest() if actual is not None else None
            if actual_hash != item.content_sha256:
                raise PolicyError(
                    f"artifact baseline mismatch at {item.destination}; "
                    "remote bytes are not the known-source build"
                )
            entries.append(
                FileEntry(
                    path=item.destination,
                    owner=item.owner,
                    content_sha256=item.content_sha256,
                    executable=item.executable,
                    exists=True,
                )
            )
        self._assert_zero_writes(transport, writes_before)
        provenance = ({
            "mode": "known_source",
            "source_tree_id": baseline.source_tree_id,
            "build_fingerprint": baseline.fingerprint,
        },)
        return ArtifactBaseline(tuple(entries), provenance)

    def verify_empty_baseline(
        self,
        project: ProjectConfig,
        transport: ArtifactBaselineTransport,
    ) -> ArtifactBaseline:
        """Prove every configured file/tree destination is absent without adopting bytes."""

        writes_before = getattr(transport, "write_calls", 0)
        for mapping in project.artifacts:
            remote = remote_path_for(project, mapping.destination)
            if mapping.kind == "file":
                present = transport.read_file(remote) is not None
            else:
                present = bool(transport.list_files(remote))
            if present:
                raise PolicyError(
                    f"empty artifact baseline refused: destination exists: {mapping.destination}"
                )
        self._assert_zero_writes(transport, writes_before)
        return ArtifactBaseline((), ({"mode": "empty"},))

    @staticmethod
    def _assert_zero_writes(transport: ArtifactReadTransport, before: int) -> None:
        """Fail if a baseline verifier ever mutates the remote transport."""

        if getattr(transport, "write_calls", 0) != before:
            raise PolicyError("artifact baseline verification performed a remote write")
