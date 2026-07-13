"""First-parent transition IDs and current-tree composition."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from typing import TYPE_CHECKING

from .errors import ConfigurationError, PolicyError
from .gitrepo import GitRepository

if TYPE_CHECKING:
    from .git_store import PersistentGitStore

ROOT_PARENT_SENTINEL = "ROOT"


@dataclass(frozen=True)
class TransitionId:
    """Identity of one first-parent commit transition event.

    Attributes:
        object_format: Git object format (``sha1`` or ``sha256``).
        commit: Full commit object id.
        first_parent: First parent id, or ``ROOT`` sentinel for root commits.
    """

    object_format: str
    commit: str
    first_parent: str

    def as_str(self) -> str:
        """Return the stable string form used in state snapshots.

        Returns:
            ``object_format:commit:first_parent`` string.
        """

        return f"{self.object_format}:{self.commit}:{self.first_parent}"

    @classmethod
    def parse(cls, value: str) -> TransitionId:
        """Parse a transition id string.

        Args:
            value: Serialized transition id.

        Returns:
            Typed transition id.
        """

        parts = value.split(":", 2)
        if len(parts) != 3:
            raise ConfigurationError(f"invalid transition id: {value!r}")
        return cls(object_format=parts[0], commit=parts[1], first_parent=parts[2])


@dataclass(frozen=True)
class ComposeResult:
    """Result of composing unapplied transitions onto a current tree.

    Attributes:
        base_tree_id: Tree id before composition (current or empty).
        target_tree_id: Tree id after applying new transitions.
        applied_transition_ids: Full ordered applied set after composition.
        introduced_transition_ids: Transitions newly introduced by this plan.
        skipped_transition_ids: Already-applied transitions that were skipped.
        commits: Resolved commit ids corresponding to introduced transitions.
        object_env: Optional Git environment able to read durable synthetic trees.
    """

    base_tree_id: str
    target_tree_id: str
    applied_transition_ids: tuple[str, ...]
    introduced_transition_ids: tuple[str, ...]
    skipped_transition_ids: tuple[str, ...]
    commits: tuple[str, ...]
    object_env: dict[str, str] | None = None
    # Process-local ephemeral object dir for plan/dry-run (not under target_root).
    # Owned solely by this result; cleaned via close() — never scan foreign temps.
    _ephemeral_dir: str | None = field(default=None, repr=False, compare=False)

    def close(self) -> None:
        """Delete the owned ephemeral plan object directory if present.

        Safe to call multiple times. Does not touch target_root durable Git
        objects or directories owned by other processes.

        Returns:
            None.
        """

        path = self._ephemeral_dir
        if not path:
            return
        try:
            shutil.rmtree(path, ignore_errors=False)
        except FileNotFoundError:
            object.__setattr__(self, "_ephemeral_dir", None)
            return
        except OSError as first_error:
            # Retry a partially removed tree, but retain ownership when cleanup
            # still fails so the caller can report/retry instead of forgetting a leak.
            try:
                shutil.rmtree(path, ignore_errors=False)
            except FileNotFoundError:
                object.__setattr__(self, "_ephemeral_dir", None)
                return
            except OSError as second_error:
                raise ConfigurationError(
                    f"failed to clean owned plan object directory {path}: {second_error}"
                ) from first_error
        object.__setattr__(self, "_ephemeral_dir", None)


class StateComposer:
    """Expand revision selectors into first-parent transitions and compose trees."""

    def __init__(
        self,
        repository: Path | GitRepository,
        *,
        git_store: PersistentGitStore | None = None,
        alternate_object_dirs: Sequence[str | Path] | None = None,
    ):
        """Bind a Git repository adapter and optional durable object store.

        Args:
            repository: Working tree path or existing adapter.
            git_store: Optional ``PersistentGitStore`` that durable-publishes
                synthetic compose objects before the temporary context exits.
            alternate_object_dirs: Read-only object directories (e.g. prior target
                store objects) used as compose alternates without writing.
        """

        self.repo = repository if isinstance(repository, GitRepository) else GitRepository(repository)
        self.git_store = git_store
        self.alternate_object_dirs = tuple(
            str(Path(item)) for item in (alternate_object_dirs or ())
        )

    def object_format(self) -> str:
        """Return the repository object format.

        Returns:
            ``sha1`` or ``sha256``.
        """

        try:
            value = self.repo._run_text("rev-parse", "--show-object-format=storage").strip()
        except ConfigurationError:
            value = "sha1"
        return value or "sha1"

    def transition_id_for_commit(self, commit: str) -> TransitionId:
        """Build a transition id for one commit.

        Args:
            commit: Resolved commit object id.

        Returns:
            Transition identity binding format/commit/first-parent.
        """

        resolved = self.repo.resolve_commit(commit)
        parent = self.repo.first_parent(resolved)
        return TransitionId(
            object_format=self.object_format(),
            commit=resolved,
            first_parent=parent if parent is not None else ROOT_PARENT_SENTINEL,
        )

    def expand_selectors(self, selectors: Sequence[str]) -> tuple[TransitionId, ...]:
        """Expand COMMIT / FROM..TO selectors into ordered unique transitions.

        Args:
            selectors: User revision selectors.

        Returns:
            Stable-ordered transition ids (oldest to newest within each range,
            selectors concatenated, de-duplicated preserving first occurrence).
        """

        ordered: list[TransitionId] = []
        seen: set[str] = set()
        for selector in selectors:
            for commit in self._expand_one(selector):
                tid = self.transition_id_for_commit(commit)
                key = tid.as_str()
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(tid)
        return tuple(ordered)

    def compose(
        self,
        *,
        selectors: Sequence[str],
        current_tree_id: str | None,
        applied_transition_ids: Sequence[str] = (),
        base_commit_for_empty: str | None = None,
    ) -> ComposeResult:
        """Compose unapplied selector transitions onto the current source tree.

        Args:
            selectors: User revision selectors.
            current_tree_id: Current composed tree, or ``None`` for empty/bootstrap.
            applied_transition_ids: Already-applied transition id strings.
            base_commit_for_empty: Optional commit used only when current is empty
                and a concrete base is required by callers.

        Returns:
            Composition result with target tree and introduced transitions.

        Raises:
            ConfigurationError: On missing dependency / apply conflict.
            PolicyError: On divergent history that cannot be composed.
        """

        expanded = self.expand_selectors(selectors)
        applied = set(applied_transition_ids)
        introduced: list[TransitionId] = []
        skipped: list[str] = []
        for tid in expanded:
            key = tid.as_str()
            if key in applied:
                skipped.append(key)
            else:
                introduced.append(tid)

        object_env: dict[str, str] | None = None
        if self.git_store is not None:
            # Prefer durable store env so current synthetic trees remain readable.
            try:
                object_env = self.git_store.object_environment()
            except Exception:
                object_env = None

        if not introduced:
            tree = current_tree_id or self.repo.empty_tree()
            return ComposeResult(
                base_tree_id=tree,
                target_tree_id=tree,
                applied_transition_ids=tuple(applied_transition_ids),
                introduced_transition_ids=(),
                skipped_transition_ids=tuple(skipped),
                commits=(),
                object_env=object_env,
            )

        commits = tuple(item.commit for item in introduced)
        if current_tree_id is None:
            base = self.repo.empty_tree()
            base_is_empty = True
        else:
            base = current_tree_id
            base_is_empty = False

        extra_alternates: list[str] = list(self.alternate_object_dirs)
        if self.git_store is not None:
            objects_dir = getattr(self.git_store, "objects_dir", None)
            if objects_dir is not None and str(objects_dir) not in extra_alternates:
                extra_alternates.append(str(objects_dir))
        ephemeral_dir: str | None = None
        try:
            with self.repo.compose_commits(
                base,
                commits,
                base_is_empty=base_is_empty,
                extra_alternates=extra_alternates or None,
            ) as (
                tree,
                env,
            ):
                target_tree = tree
                env = dict(env)
                env["_tree_id"] = tree
                # Durable target store only when bound (real deploy path).
                if self.git_store is not None:
                    self.git_store.write_tree_objects(env)
                    object_env = self.git_store.object_environment()
                else:
                    # Plan/dry-run: keep objects in process-local temp, not target_root.
                    # Owner-only permissions; cleaned by ComposeResult.close().
                    ephemeral_dir = tempfile.mkdtemp(prefix="git-deploy-plan-")
                    try:
                        os.chmod(ephemeral_dir, 0o700)
                    except OSError:
                        pass
                    eph_objects = Path(ephemeral_dir) / "objects"
                    eph_objects.mkdir(parents=True, exist_ok=True)
                    src_objects = Path(env["GIT_OBJECT_DIRECTORY"])
                    if src_objects.is_dir():
                        for path in src_objects.rglob("*"):
                            if path.is_file():
                                rel = path.relative_to(src_objects)
                                dest = eph_objects / rel
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(path, dest)
                    object_env = dict(env)
                    object_env["GIT_OBJECT_DIRECTORY"] = str(eph_objects)
                    # Preserve alternates so main-repo + prior objects remain readable.
        except Exception:
            # Composition failed before result ownership transfer — delete owned temp.
            if ephemeral_dir is not None:
                shutil.rmtree(ephemeral_dir, ignore_errors=True)
            raise

        new_applied = list(applied_transition_ids)
        for tid in introduced:
            key = tid.as_str()
            if key not in applied:
                new_applied.append(key)
                applied.add(key)

        return ComposeResult(
            base_tree_id=base,
            target_tree_id=target_tree,
            applied_transition_ids=tuple(new_applied),
            introduced_transition_ids=tuple(tid.as_str() for tid in introduced),
            skipped_transition_ids=tuple(skipped),
            commits=commits,
            object_env=object_env,
            _ephemeral_dir=ephemeral_dir,
        )

    def detect_divergence(self, left: str, right: str) -> None:
        """Fail when two commits are not on a shared first-parent line.

        Args:
            left: Commit a.
            right: Commit b.

        Returns:
            None.
        """

        try:
            self.repo.require_ancestor(left, right)
            return
        except ConfigurationError:
            pass
        try:
            self.repo.require_ancestor(right, left)
            return
        except ConfigurationError as exc:
            raise PolicyError(
                f"selected revisions diverge: {left[:12]} and {right[:12]} share no first-parent ancestry"
            ) from exc

    def _expand_one(self, selector: str) -> tuple[str, ...]:
        """Expand one selector into oldest→newest commits.

        Args:
            selector: ``COMMIT`` or ``FROM..TO`` / ``FROM...TO``.

        Returns:
            Ordered commit ids.
        """

        raw = selector.strip()
        if not raw:
            raise ConfigurationError("empty revision selector")
        if "..." in raw:
            left, _, right = raw.partition("...")
            return self._expand_range(left, right)
        if ".." in raw:
            left, _, right = raw.partition("..")
            return self._expand_range(left, right)
        return (self.repo.resolve_commit(raw),)

    def _expand_range(self, older: str, newer: str) -> tuple[str, ...]:
        """Expand a first-parent range.

        Args:
            older: Range start (exclusive).
            newer: Range end (inclusive).

        Returns:
            Commits oldest to newest.
        """

        if not older.strip() or not newer.strip():
            raise ConfigurationError(f"invalid range selector: {older}..{newer}")
        older_id = self.repo.resolve_commit(older)
        newer_id = self.repo.resolve_commit(newer)
        try:
            return self.repo.first_parent_range(older_id, newer_id)
        except ConfigurationError as exc:
            # Surface diverge/dependency failures before any remote connection.
            raise ConfigurationError(str(exc)) from exc


def stable_transition_order(ids: Sequence[str]) -> tuple[str, ...]:
    """Return transition ids with de-duplication preserving first occurrence.

    Args:
        ids: Transition id strings.

    Returns:
        Ordered unique ids.
    """

    seen: set[str] = set()
    ordered: list[str] = []
    for item in ids:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)
