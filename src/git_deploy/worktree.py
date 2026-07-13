"""Isolated build worktrees materialized from exact Git tree objects."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass
class MaterializedWorktree:
    """Owned worktree directory whose lifecycle is explicit and retryable."""

    path: Path
    tree_id: str
    _index_path: Path
    _closed: bool = False

    def close(self) -> None:
        """Remove only this worktree and its temporary index.

        Returns:
            None.

        Raises:
            ConfigurationError: When owned files cannot be removed.
        """

        if self._closed:
            return
        errors: list[str] = []
        try:
            shutil.rmtree(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"worktree {self.path}: {exc}")
        try:
            self._index_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"index {self._index_path}: {exc}")
        if errors:
            raise ConfigurationError("failed to clean isolated build inputs: " + "; ".join(errors))
        self._closed = True


class WorktreeManager:
    """Materialize a Git tree without touching the repository index/worktree."""

    def __init__(self, repository: Path, base_dir: Path):
        """Bind a source repository and owner-only temporary directory.

        Args:
            repository: Git working tree used to resolve ordinary objects.
            base_dir: Directory that will own temporary worktrees and indexes.
        """

        self.repository = repository.resolve()
        self.base_dir = base_dir.resolve()

    @contextmanager
    def materialize(
        self,
        tree_id: str,
        *,
        object_env: Mapping[str, str] | None = None,
    ) -> Iterator[MaterializedWorktree]:
        """Yield an exact isolated checkout for a real or persistent synthetic tree.

        Args:
            tree_id: Tree object to materialize.
            object_env: Optional persistent Git object/alternate environment.

        Yields:
            Owned materialized worktree, removed on every normal exception path.
        """

        self.base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.base_dir.chmod(0o700)
        except OSError:
            pass
        worktree_path = Path(
            tempfile.mkdtemp(prefix="git-deploy-worktree-", dir=self.base_dir)
        )
        index_fd, index_name = tempfile.mkstemp(prefix=".git-deploy-index-", dir=self.base_dir)
        os.close(index_fd)
        index_path = Path(index_name)
        index_path.unlink()
        worktree = MaterializedWorktree(worktree_path, tree_id, index_path)
        env = os.environ.copy()
        if object_env is not None:
            env.update({str(key): str(value) for key, value in object_env.items()})
        env["GIT_INDEX_FILE"] = str(index_path)
        try:
            self._git(env, "read-tree", tree_id)
            actual = self._git(env, "write-tree").stdout.decode().strip()
            if actual != tree_id:
                raise ConfigurationError(
                    f"isolated worktree index mismatch: expected {tree_id}, got {actual}"
                )
            prefix = str(worktree_path) + os.sep
            self._git(env, "checkout-index", "--all", "--force", f"--prefix={prefix}")
            yield worktree
        finally:
            worktree.close()

    def _git(self, env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
        """Run one non-interactive Git command against the source object database."""

        proc = subprocess.run(
            ["git", "-C", str(self.repository), *args],
            check=False,
            capture_output=True,
            env=dict(env),
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ConfigurationError(f"cannot materialize build tree with git {' '.join(args)}: {detail}")
        return proc
