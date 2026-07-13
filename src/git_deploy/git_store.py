"""Persistent Git object store for composed trees with alternate validation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .durable_io import durable_publish, ensure_state_directory
from .errors import ConfigurationError
from .gitrepo import GitRepository


class PersistentGitStore:
    """Store composed Git objects under ``git/objects`` with main-repo alternates."""

    def __init__(self, target_root: Path, repository: Path):
        """Bind a target git object store to a source repository.

        Args:
            target_root: ``.../targets/<target-id>`` directory.
            repository: Project Git working tree used as alternate.
        """

        self.root = target_root.resolve()
        self.repository = repository.resolve()
        self.git_dir = self.root / "git"
        self.objects_dir = self.git_dir / "objects"
        self._repo = GitRepository(self.repository)

    def ensure_layout(self) -> None:
        """Create the git object directory layout.

        Returns:
            None.
        """

        ensure_state_directory(self.root)
        ensure_state_directory(self.git_dir)
        ensure_state_directory(self.objects_dir)

    def repository_identity(self) -> str:
        """Return a stable identity for the main repository object database.

        Returns:
            Resolved main objects directory path string.
        """

        return str(self._main_objects())

    def write_tree_objects(self, tree_env: dict[str, str]) -> str:
        """Copy temporary compose objects into the durable store.

        Args:
            tree_env: Environment from ``GitRepository.compose_commits`` containing
                ``GIT_OBJECT_DIRECTORY`` with newly written objects.

        Returns:
            Tree id that remains readable via this store's environment.
        """

        self.ensure_layout()
        temporary_objects = tree_env.get("GIT_OBJECT_DIRECTORY")
        if not temporary_objects:
            raise ConfigurationError("compose environment missing GIT_OBJECT_DIRECTORY")
        source = Path(temporary_objects)
        if not source.is_dir():
            raise ConfigurationError(f"temporary git objects missing: {source}")
        self._durable_copy_objects(source)
        self._publish_repository_identity()
        return tree_env.get("_tree_id", "")

    def persist_tree_from_compose(
        self,
        base: str,
        commits: list[str] | tuple[str, ...],
        *,
        base_is_empty: bool = False,
    ) -> str:
        """Compose commits and persist resulting objects for cross-process reads.

        Args:
            base: Base commit or empty tree id.
            commits: Ordered commits to apply.
            base_is_empty: Whether base is the empty tree.

        Returns:
            Resulting tree object id readable from a new process.
        """

        with self._repo.compose_commits(base, commits, base_is_empty=base_is_empty) as (
            tree,
            environment,
        ):
            self.ensure_layout()
            temporary_objects = Path(environment["GIT_OBJECT_DIRECTORY"])
            self._durable_copy_objects(temporary_objects)
            self._publish_repository_identity()
            # Verify tree is readable through persistent store env.
            self.require_tree(tree)
            return tree

    def object_environment(self) -> dict[str, str]:
        """Build an environment that reads durable objects plus main alternates.

        Returns:
            Environment mapping suitable for Git subprocesses.
        """

        self.ensure_layout()
        self._assert_repository_identity()
        environment = os.environ.copy()
        main_objects = self._main_objects()
        environment["GIT_OBJECT_DIRECTORY"] = str(self.objects_dir)
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(main_objects)
        return environment

    def require_tree(self, tree_id: str) -> None:
        """Require that a tree id is readable via the durable store.

        Args:
            tree_id: Git tree object identifier.

        Returns:
            None.
        """

        env = self.object_environment()
        proc = subprocess.run(
            ["git", "-C", str(self.repository), "cat-file", "-t", tree_id],
            check=False,
            capture_output=True,
            env=env,
        )
        if proc.returncode != 0 or proc.stdout.decode().strip() != "tree":
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ConfigurationError(
                f"persistent git store cannot read tree {tree_id[:12]}: {detail}"
            )

    def main_object_count(self) -> int:
        """Count objects under the main repository object database.

        Returns:
            Number of files under main ``objects/`` (excluding info/pack dirs counts as files).
        """

        main = self._main_objects()
        if not main.is_dir():
            return 0
        return sum(1 for path in main.rglob("*") if path.is_file())

    def _main_objects(self) -> Path:
        """Resolve the main repository objects directory.

        Returns:
            Absolute objects path.
        """

        proc = subprocess.run(
            ["git", "-C", str(self.repository), "rev-parse", "--git-path", "objects"],
            check=True,
            capture_output=True,
            text=True,
        )
        path = Path(proc.stdout.strip())
        if not path.is_absolute():
            path = (self.repository / path).resolve()
        return path

    def _assert_repository_identity(self) -> None:
        """Block reads when the main repository objects path no longer matches.

        Returns:
            None.
        """

        identity_path = self.git_dir / "repository_identity"
        if not identity_path.is_file():
            return
        recorded = identity_path.read_text(encoding="utf-8").strip()
        actual = self.repository_identity()
        if recorded != actual:
            raise ConfigurationError(
                "persistent git store repository identity mismatch; "
                f"recorded={recorded!r} actual={actual!r}"
            )

    def _durable_copy_objects(self, source: Path) -> None:
        """Publish temporary Git objects via durable write→fsync→replace→dir-fsync.

        Args:
            source: Temporary object directory from compose.

        Returns:
            None.
        """

        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            destination = self.objects_dir / relative
            ensure_state_directory(destination.parent)
            if destination.exists():
                continue
            durable_publish(destination, path.read_bytes())

    def _publish_repository_identity(self) -> None:
        """Durably publish repository identity and path markers.

        Returns:
            None.
        """

        identity_path = self.git_dir / "repository_identity"
        durable_publish(identity_path, (self.repository_identity() + "\n").encode("utf-8"))
        repo_marker = self.git_dir / "repository_path"
        durable_publish(repo_marker, (str(self.repository) + "\n").encode("utf-8"))
