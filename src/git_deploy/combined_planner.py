"""Merge source and artifact plans into one owner-safe target state."""

from __future__ import annotations

from dataclasses import dataclass

from .artifact_planner import ArtifactPlan
from .errors import PolicyError
from .expected_state import FileEntry
from .models import PlannedFile
from .state_planner import SourceDiffPlan


@dataclass(frozen=True)
class CombinedPlan:
    """Unified remote mutations, target file table, and artifact content refs."""

    files: tuple[PlannedFile, ...]
    target_entries: tuple[FileEntry, ...]
    artifact_content_refs: tuple[tuple[str, str], ...]


class CombinedPlanner:
    """Apply both domains to current state and reject owner/path collisions."""

    def combine(
        self,
        current_entries: tuple[FileEntry, ...],
        source: SourceDiffPlan,
        artifacts: ArtifactPlan,
    ) -> CombinedPlan:
        """Build one target state and mutation list before any remote connection.

        Args:
            current_entries: Complete current managed file table.
            source: Source plan relative to current source tree.
            artifacts: Trusted artifact plan.

        Returns:
            Unified plan with stable path ordering.
        """

        if artifacts.status != "ready":
            raise PolicyError(artifacts.reason or "artifact baseline required")
        table = {entry.path: entry for entry in current_entries}
        for operation in source.files:
            existing = table.get(operation.path)
            if existing is not None and existing.owner != "source":
                raise PolicyError(
                    f"source/artifact owner conflict at {operation.path}: {existing.owner}"
                )
            if operation.action == "delete":
                table.pop(operation.path, None)
            else:
                table[operation.path] = FileEntry(
                    path=operation.path,
                    owner="source",
                    content_sha256=operation.target_sha256,
                    executable=operation.executable,
                    exists=True,
                )

        # Target artifact table is authoritative: remove all prior artifact entries.
        table = {
            path: entry
            for path, entry in table.items()
            if not entry.owner.startswith("artifact:")
        }
        seen_artifact_paths: set[str] = set()
        for entry in artifacts.target_entries:
            if entry.path in seen_artifact_paths:
                raise PolicyError(f"artifact/artifact destination conflict: {entry.path}")
            seen_artifact_paths.add(entry.path)
            existing = table.get(entry.path)
            if existing is not None and existing.owner != entry.owner:
                raise PolicyError(
                    f"source/artifact owner conflict at {entry.path}: "
                    f"{existing.owner} vs {entry.owner}"
                )
            table[entry.path] = entry

        self._assert_no_hierarchy_conflicts(tuple(table.values()))
        operations = tuple(sorted(source.files + artifacts.files, key=lambda item: item.path))
        operation_paths: set[str] = set()
        for item in operations:
            if item.path in operation_paths:
                raise PolicyError(f"combined plan mutates path twice: {item.path}")
            operation_paths.add(item.path)
        return CombinedPlan(
            files=operations,
            target_entries=tuple(table[path] for path in sorted(table)),
            artifact_content_refs=artifacts.content_refs,
        )

    @staticmethod
    def _assert_no_hierarchy_conflicts(entries: tuple[FileEntry, ...]) -> None:
        """Reject file paths where one managed owner is another path's ancestor."""

        ordered = sorted(entries, key=lambda item: item.path)
        for index, left in enumerate(ordered):
            prefix = left.path.rstrip("/") + "/"
            for right in ordered[index + 1 :]:
                if not right.path.startswith(prefix):
                    continue
                if left.owner != right.owner:
                    raise PolicyError(
                        f"managed owner hierarchy conflict: {left.path} ({left.owner}) "
                        f"contains {right.path} ({right.owner})"
                    )
