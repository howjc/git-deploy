"""Legacy and named-remote history migration: plan → staging → durable publish."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .durable_io import durable_publish, ensure_state_directory
from .errors import ConfigurationError, PolicyError
from .target_identity import TargetIdentity
from .target_lock import TargetLock


@dataclass
class MigrationItem:
    """One discovered legacy history location.

    Attributes:
        alias: Remote alias or ``default``.
        source_dir: Legacy deployments directory.
        target_id: Physical target id.
        conflict: Conflict reason if any.
    """

    alias: str
    source_dir: Path
    target_id: str
    conflict: str | None = None


@dataclass
class MigrationPlan:
    """Dry-run migration plan.

    Attributes:
        items: Discovered items.
        shared_targets: target_id → aliases sharing it.
        blocked: Whether plan is blocked.
        reasons: Block reasons.
    """

    items: list[MigrationItem] = field(default_factory=list)
    shared_targets: dict[str, list[str]] = field(default_factory=dict)
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)


class StateMigrationService:
    """Migrate ``<project>/deployments`` and ``remotes/<alias>`` into target-id layout."""

    def __init__(self, project_state_base: Path):
        """Bind the project-level state base (not yet target-id rooted).

        Args:
            project_state_base: e.g. ``.state/demo`` containing deployments/remotes.
        """

        self.base = project_state_base.resolve()

    def plan(self, alias_to_identity: dict[str, TargetIdentity]) -> MigrationPlan:
        """Discover legacy history and group by physical target.

        Args:
            alias_to_identity: Mapping of remote alias → identity.

        Returns:
            Migration plan; blocked on conflicts.
        """

        plan = MigrationPlan()
        # Legacy default deployments.
        default_deployments = self.base / "deployments"
        if default_deployments.is_dir() and "default" in alias_to_identity:
            identity = alias_to_identity["default"]
            plan.items.append(
                MigrationItem(
                    alias="default",
                    source_dir=default_deployments,
                    target_id=identity.target_id,
                )
            )
        remotes_root = self.base / "remotes"
        if remotes_root.is_dir():
            for alias_dir in sorted(remotes_root.iterdir()):
                if not alias_dir.is_dir():
                    continue
                alias = alias_dir.name
                deployments = alias_dir / "deployments"
                if not deployments.is_dir():
                    continue
                identity = alias_to_identity.get(alias)
                if identity is None:
                    plan.blocked = True
                    plan.reasons.append(f"no identity for alias {alias}")
                    continue
                plan.items.append(
                    MigrationItem(
                        alias=alias,
                        source_dir=deployments,
                        target_id=identity.target_id,
                    )
                )

        shared: dict[str, list[str]] = {}
        for item in plan.items:
            shared.setdefault(item.target_id, []).append(item.alias)
        plan.shared_targets = shared

        # Conflict: two aliases same target with incompatible manifest ids content.
        for target_id, aliases in shared.items():
            if len(aliases) < 2:
                continue
            sources = [item for item in plan.items if item.target_id == target_id]
            if self._sources_conflict(sources):
                plan.blocked = True
                reason = f"incompatible history for target {target_id} aliases {aliases}"
                plan.reasons.append(reason)
                for item in sources:
                    item.conflict = reason
        return plan

    def stage(self, plan: MigrationPlan, staging_root: Path) -> Path:
        """Copy planned history into an isolated staging tree and verify.

        Args:
            plan: Migration plan (must not be blocked).
            staging_root: Isolated staging directory.

        Returns:
            Staging root path.

        Raises:
            PolicyError: When plan is blocked.
            ConfigurationError: When verification fails (staging removed).
        """

        if plan.blocked:
            raise PolicyError("migration plan blocked: " + "; ".join(plan.reasons))
        if staging_root.exists():
            shutil.rmtree(staging_root)
        ensure_state_directory(staging_root)
        try:
            for item in plan.items:
                dest = staging_root / "targets" / item.target_id / "deployments"
                ensure_state_directory(dest)
                for entry in item.source_dir.iterdir():
                    target = dest / entry.name
                    if entry.is_dir():
                        if target.exists():
                            # Same deployment id from two aliases: require identical content.
                            if not _dirs_equal(entry, target):
                                raise ConfigurationError(
                                    f"staging conflict for {item.target_id} deployment {entry.name}"
                                )
                        else:
                            shutil.copytree(entry, target)
            # Write staging marker.
            durable_publish(
                staging_root / "staging.json",
                json.dumps(
                    {"items": [item.alias for item in plan.items], "status": "verified"},
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n",
            )
            return staging_root
        except Exception:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            raise

    def publish(self, staging_root: Path, *, yes: bool = False) -> Path:
        """Publish staging into live target directories under target lock.

        Args:
            staging_root: Verified staging tree.
            yes: Required confirmation.

        Returns:
            Path to the durable migration record.

        Raises:
            ConfigurationError: When yes is false or staging missing.
        """

        if not yes:
            raise ConfigurationError("migration publish requires --yes")
        marker = staging_root / "staging.json"
        if not marker.is_file():
            raise ConfigurationError("staging verification marker missing")
        targets = staging_root / "targets"
        if not targets.is_dir():
            raise ConfigurationError("staging targets missing")

        # Publish each target deployments tree.
        for target_dir in sorted(targets.iterdir()):
            if not target_dir.is_dir():
                continue
            live = self.base / "targets" / target_dir.name
            with TargetLock(live):
                ensure_state_directory(live)
                live_deployments = live / "deployments"
                ensure_state_directory(live_deployments)
                staged_deployments = target_dir / "deployments"
                if staged_deployments.is_dir():
                    for entry in staged_deployments.iterdir():
                        dest = live_deployments / entry.name
                        if dest.exists():
                            continue
                        if entry.is_dir():
                            shutil.copytree(entry, dest)
                record = {
                    "target_id": target_dir.name,
                    "status": "published",
                    "legacy_preserved": True,
                }
                durable_publish(
                    live / "migration.json",
                    json.dumps(record, sort_keys=True).encode("utf-8") + b"\n",
                )
        # Do not delete legacy evidence.
        return self.base / "targets"

    def _sources_conflict(self, sources: list[MigrationItem]) -> bool:
        """Return whether sources for one target have conflicting deployment payloads.

        Args:
            sources: Items sharing a target id.

        Returns:
            ``True`` when same deployment id has different content.
        """

        by_id: dict[str, Path] = {}
        for item in sources:
            if not item.source_dir.is_dir():
                continue
            for entry in item.source_dir.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name in by_id and not _dirs_equal(by_id[entry.name], entry):
                    return True
                by_id.setdefault(entry.name, entry)
        return False


def _dirs_equal(left: Path, right: Path) -> bool:
    """Compare two directories for equal file contents.

    Args:
        left: First directory.
        right: Second directory.

    Returns:
        Equality flag.
    """

    left_files = {p.relative_to(left): p for p in left.rglob("*") if p.is_file()}
    right_files = {p.relative_to(right): p for p in right.rglob("*") if p.is_file()}
    if set(left_files) != set(right_files):
        return False
    for rel, path in left_files.items():
        if path.read_bytes() != right_files[rel].read_bytes():
            return False
    return True
