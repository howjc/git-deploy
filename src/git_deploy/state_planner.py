"""Source file state diffs and static no-op planning from current trees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from typing import TYPE_CHECKING

from .expected_state import FileEntry
from .gitrepo import GitRepository
from .models import GitChange, PlannedFile
from .state_composer import ComposeResult, StateComposer

if TYPE_CHECKING:
    from .git_store import PersistentGitStore


@dataclass(frozen=True)
class SourceDiffPlan:
    """Planned source mutations relative to a current expected tree.

    Attributes:
        before_tree_id: Current source tree id.
        after_tree_id: Target source tree id.
        files: Planned remote path mutations.
        excluded: Changes excluded by include/exclude policy.
        introduced_transition_ids: New transitions in this plan.
        applied_transition_ids: Full applied set after success.
        remote_unverified: True when plan is static/local-only (no remote check).
        static_noop: True when no file mutations and only already-applied transitions.
        revision_specs: Durable selectors with HEAD expressions frozen to commit IDs.
        expected_before_state_id: Plan-time current state id (lock-held stale guard).
        expected_generation: Plan-time current generation (lock-held stale guard).
        expected_before_tree_id: Optional explicit freeze of before tree (defaults to before_tree_id).
        expected_before_applied_transition_ids: Plan-time applied transition set.
    """

    before_tree_id: str
    after_tree_id: str
    files: tuple[PlannedFile, ...]
    excluded: tuple[GitChange, ...]
    introduced_transition_ids: tuple[str, ...]
    applied_transition_ids: tuple[str, ...]
    remote_unverified: bool = False
    static_noop: bool = False
    revision_specs: tuple[str, ...] = ()
    expected_before_state_id: str | None = None
    expected_generation: int | None = None
    expected_before_tree_id: str | None = None
    expected_before_applied_transition_ids: tuple[str, ...] | None = None


class StatePlanner:
    """Build source diffs from current/target trees using project path policy."""

    def __init__(
        self,
        repository: Path | GitRepository,
        *,
        include: Sequence[str] = ("**",),
        exclude: Sequence[str] = (),
        protected: Sequence[str] = (),
        remote_root: str = "/",
        git_store: PersistentGitStore | None = None,
        alternate_object_dirs: Sequence[str | Path] | None = None,
    ):
        """Bind repository and path policy.

        Args:
            repository: Git working tree or adapter.
            include: Include globs.
            exclude: Exclude globs.
            protected: Protected path globs (never mutated).
            remote_root: Absolute remote root for remote_path construction.
            git_store: Optional durable Git object store for synthetic trees.
            alternate_object_dirs: Read-only object dirs for plan-time compose.
        """

        self.repo = repository if isinstance(repository, GitRepository) else GitRepository(repository)
        self.git_store = git_store
        self.composer = StateComposer(
            self.repo,
            git_store=git_store,
            alternate_object_dirs=alternate_object_dirs,
        )
        self.include = tuple(include)
        self.exclude = tuple(exclude)
        self.protected = tuple(protected)
        self.remote_root = remote_root.rstrip("/") or ""
        self._object_env: dict[str, str] | None = None

    def plan_from_compose(
        self,
        compose: ComposeResult,
        *,
        remote_unverified: bool = False,
        close_ephemeral: bool = True,
        revision_specs: Sequence[str] = (),
    ) -> SourceDiffPlan:
        """Build a source diff plan from a composition result.

        When ``close_ephemeral`` is true (default), any plan-only temporary
        object directory owned by ``compose`` is deleted after tree/blob reads
        complete so `/tmp/git-deploy-plan-*` does not leak. Durable deploy
        results have no ephemeral dir.

        Args:
            compose: Composer output.
            remote_unverified: Mark plan as local-only static.
            close_ephemeral: Close owned ephemeral object dir after planning.
            revision_specs: Immutable selectors recorded in deployment history.

        Returns:
            Source diff plan with before=current tree semantics.
        """

        try:
            self._object_env = compose.object_env
            if self._object_env is None and self.git_store is not None:
                try:
                    self._object_env = self.git_store.object_environment()
                except Exception:
                    self._object_env = None

            if compose.base_tree_id == compose.target_tree_id and not compose.introduced_transition_ids:
                return SourceDiffPlan(
                    before_tree_id=compose.base_tree_id,
                    after_tree_id=compose.target_tree_id,
                    files=(),
                    excluded=(),
                    introduced_transition_ids=(),
                    applied_transition_ids=compose.applied_transition_ids,
                    remote_unverified=remote_unverified,
                    static_noop=True,
                    revision_specs=tuple(revision_specs),
                )

            changes = self.repo.changes(
                compose.base_tree_id,
                compose.target_tree_id,
                env=self._object_env,
            )
            planned: list[PlannedFile] = []
            excluded: list[GitChange] = []
            for change in changes:
                path = change.path
                if self._is_protected(path) or not self._is_included(path):
                    excluded.append(change)
                    continue
                planned.append(
                    self._plan_change(change, compose.base_tree_id, compose.target_tree_id)
                )

            return SourceDiffPlan(
                before_tree_id=compose.base_tree_id,
                after_tree_id=compose.target_tree_id,
                files=tuple(planned),
                excluded=tuple(excluded),
                introduced_transition_ids=compose.introduced_transition_ids,
                applied_transition_ids=compose.applied_transition_ids,
                remote_unverified=remote_unverified,
                static_noop=not planned and not compose.introduced_transition_ids,
                revision_specs=tuple(revision_specs),
            )
        finally:
            # Lifecycle covers all tree/blob reads above; drop env if it pointed
            # at the deleted ephemeral directory (plan/dry-run only).
            if close_ephemeral and compose._ephemeral_dir is not None:
                compose.close()
                if self.git_store is None:
                    self._object_env = None

    def plan_selectors(
        self,
        selectors: Sequence[str],
        *,
        current_tree_id: str | None,
        applied_transition_ids: Sequence[str] = (),
        static_only: bool = False,
    ) -> SourceDiffPlan:
        """Compose selectors and produce a source plan.

        Args:
            selectors: Revision selectors.
            current_tree_id: Current source tree.
            applied_transition_ids: Already applied transitions.
            static_only: When true, mark remote_unverified (plan/dry-run).

        Returns:
            Source diff plan.
        """

        revision_specs = self.repo.freeze_head_revision_specs(selectors)
        compose = self.composer.compose(
            selectors=revision_specs,
            current_tree_id=current_tree_id,
            applied_transition_ids=applied_transition_ids,
        )
        return self.plan_from_compose(
            compose,
            remote_unverified=static_only,
            revision_specs=revision_specs,
        )

    def file_entries_for_tree(self, tree_id: str, paths: Sequence[str] | None = None) -> tuple[FileEntry, ...]:
        """Materialize managed file entries from a Git tree.

        Args:
            tree_id: Git tree/commit-ish.
            paths: Optional path filter; default lists all regular files via ls-tree.

        Returns:
            File entries owned by ``source``.
        """

        env = self._resolve_object_env()
        if paths is None:
            raw = self.repo._run("ls-tree", "-r", "--name-only", tree_id, env=env).stdout
            listed = raw.decode("utf-8", errors="replace").splitlines()
            paths = [item for item in listed if item]
        entries: list[FileEntry] = []
        for path in paths:
            if not self._is_included(path) or self._is_protected(path):
                continue
            blob = self.repo.blob(tree_id, path, env=env)
            if blob is None:
                entries.append(
                    FileEntry(path=path, owner="source", content_sha256=None, exists=False)
                )
            else:
                entries.append(
                    FileEntry(
                        path=path,
                        owner="source",
                        content_sha256=blob.sha256,
                        executable=blob.executable,
                        exists=True,
                    )
                )
        return tuple(entries)

    def object_env(self) -> dict[str, str] | None:
        """Return the object environment used for the last plan/compose.

        Returns:
            Environment mapping or ``None`` when only the main repo is needed.
        """

        return self._resolve_object_env()

    def _resolve_object_env(self) -> dict[str, str] | None:
        """Resolve the durable object environment for tree/blob reads.

        Returns:
            Environment mapping or ``None``.
        """

        if self._object_env is not None:
            return self._object_env
        if self.git_store is not None:
            try:
                self._object_env = self.git_store.object_environment()
            except Exception:
                return None
        return self._object_env

    def _plan_change(self, change: GitChange, before_tree: str, after_tree: str) -> PlannedFile:
        """Convert one Git change into a PlannedFile using tree baselines.

        Args:
            change: Normalized Git change.
            before_tree: Current tree id.
            after_tree: Target tree id.

        Returns:
            Planned file mutation.
        """

        path = change.path
        remote_path = f"{self.remote_root}/{path}" if self.remote_root else f"/{path}"
        env = self._resolve_object_env()
        before_blob = self.repo.blob(before_tree, change.old_path or path, env=env)
        after_blob = (
            None
            if change.status.startswith("D")
            else self.repo.blob(after_tree, path, env=env)
        )
        if change.status.startswith("A") or change.status == "?":
            action = "upload"
        elif change.status.startswith("D"):
            action = "delete"
        elif change.status.startswith("R") or change.status.startswith("C"):
            action = "upload"
        else:
            action = "upload"
        return PlannedFile(
            action=action if after_blob is not None else "delete",
            path=path,
            remote_path=remote_path,
            source_path=path if after_blob is not None else None,
            expected_before_sha256=before_blob.sha256 if before_blob else None,
            target_sha256=after_blob.sha256 if after_blob else None,
            target_size=len(after_blob.data) if after_blob else 0,
            executable=after_blob.executable if after_blob else False,
            expected_before_executable=before_blob.executable if before_blob else None,
        )

    def _is_included(self, path: str) -> bool:
        """Return whether a path matches include and not exclude.

        Args:
            path: Repository-relative path.

        Returns:
            Inclusion decision.
        """

        from fnmatch import fnmatch

        included = any(fnmatch(path, pattern) or fnmatch(path, pattern.rstrip("/")) for pattern in self.include)
        if not included and "**" not in self.include:
            # Also allow directory-prefix style includes.
            included = any(path.startswith(pattern.rstrip("*").rstrip("/")) for pattern in self.include if pattern)
        if self.include == ("**",) or not self.include:
            included = True
        if any(fnmatch(path, pattern) for pattern in self.exclude):
            return False
        return included

    def _is_protected(self, path: str) -> bool:
        """Return whether a path is protected.

        Args:
            path: Repository-relative path.

        Returns:
            ``True`` when protected.
        """

        from fnmatch import fnmatch

        return any(fnmatch(path, pattern) for pattern in self.protected)
