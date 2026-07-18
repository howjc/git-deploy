"""Read committed source history and materialize exact HEAD blobs."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from git_deploy.errors import PlanError
from git_deploy.manifest import ManifestEntry


@dataclass(frozen=True, slots=True)
class GitChange:
    """Represent one add, modify, or delete between two commits."""

    status: str
    path: str


@dataclass(frozen=True, slots=True)
class GitEntry:
    """Represent one committed tree entry and its Git mode."""

    path: str
    mode: str


class GitRepository:
    """Provide the narrow Git operations required by v1-lite."""

    def __init__(self, root: Path) -> None:
        """Bind Git commands to one project root.

        Args:
            root: Directory expected to belong to a Git worktree.
        """

        self.root = root.resolve()

    def validate(self) -> None:
        """Raise a plan error unless the root is a Git worktree."""

        output = self._run("rev-parse", "--is-inside-work-tree")
        if output.strip() != b"true":
            raise PlanError(f"not a Git worktree: {self.root}")

    def head(self) -> str:
        """Return the full current HEAD object ID."""

        return self._run("rev-parse", "--verify", "HEAD").decode().strip()

    def git_dir(self) -> Path:
        """Return the resolved per-worktree Git metadata directory."""

        value = self._run("rev-parse", "--git-dir").decode().strip()
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    def common_dir(self) -> Path:
        """Return the shared Git metadata directory used by all linked worktrees."""

        value = self._run("rev-parse", "--git-common-dir").decode().strip()
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

    def is_dirty(self) -> bool:
        """Return whether tracked or untracked worktree changes exist."""

        return bool(self.status_porcelain())

    def status_porcelain(self) -> bytes:
        """Return stable porcelain status for pre/post-build comparison."""

        return self._run("status", "--porcelain", "--untracked-files=normal")

    def is_ignored(self, path: Path) -> bool:
        """Return whether Git ignore rules cover one project-local path.

        Args:
            path: Absolute or project-relative local path.

        Returns:
            ``True`` only when ``git check-ignore`` confirms the path is ignored.
        """

        candidate = path.resolve() if path.is_absolute() else (self.root / path).resolve()
        if not candidate.is_relative_to(self.root):
            raise PlanError(f"cannot inspect ignore rules outside the project: {path}")
        relative = candidate.relative_to(self.root).as_posix()
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", relative],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode not in {0, 1}:
            detail = result.stderr.decode(errors="replace").strip()
            raise PlanError(f"Git check-ignore failed for {relative!r}: {detail}")
        return result.returncode == 0

    def commit_exists(self, commit: str) -> bool:
        """Return whether a state commit still resolves to a commit object.

        Args:
            commit: Full or abbreviated Git object ID read from state.

        Returns:
            ``True`` only when Git verifies a commit object.
        """

        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def list_head_entries(self) -> tuple[GitEntry, ...]:
        """Return every file-like entry tracked by HEAD in deterministic order."""

        return self.list_entries("HEAD")

    def list_entries(self, commit: str) -> tuple[GitEntry, ...]:
        """Return every file-like entry tracked by one commit.

        Args:
            commit: Commit whose recursive tree should be inspected.

        Returns:
            Deterministically sorted file-like Git entries.
        """

        raw = self._run("ls-tree", "-r", "-z", commit)
        entries: list[GitEntry] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                metadata, path_raw = record.split(b"\t", 1)
                mode = metadata.split(b" ", 1)[0].decode("ascii")
                path = os.fsdecode(path_raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise PlanError("Git returned an invalid tree record") from exc
            entries.append(GitEntry(path, mode))
        return tuple(sorted(entries, key=lambda item: item.path))

    def diff(self, old_commit: str, new_commit: str) -> tuple[GitChange, ...]:
        """Return add/modify/delete changes with rename detection disabled.

        Args:
            old_commit: Last successfully deployed commit.
            new_commit: Current HEAD commit.

        Returns:
            Deterministically sorted source changes.
        """

        if not self.commit_exists(old_commit):
            raise PlanError(
                f"state commit {old_commit!r} is unavailable; rerun with --full to rebuild state"
            )
        raw = self._run(
            "diff",
            "--no-renames",
            "--name-status",
            "-z",
            f"{old_commit}..{new_commit}",
        )
        tokens = [token for token in raw.split(b"\0") if token]
        changes: list[GitChange] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if b"\t" in token:
                status_raw, path_raw = token.split(b"\t", 1)
                index += 1
            else:
                if index + 1 >= len(tokens):
                    raise PlanError("Git returned a truncated name-status record")
                status_raw, path_raw = token, tokens[index + 1]
                index += 2
            status = status_raw.decode("ascii", errors="strict")[:1]
            if status not in {"A", "M", "D"}:
                raise PlanError(f"unsupported Git diff status: {status_raw!r}")
            changes.append(GitChange(status, os.fsdecode(path_raw)))
        return tuple(sorted(changes, key=lambda item: (item.path, item.status)))

    def export_file(self, commit: str, path: str, destination: Path) -> None:
        """Write one exact committed blob to a local staging path.

        Args:
            commit: Frozen commit object ID captured by the deployment plan.
            path: Relative Git path selected by the source planner.
            destination: Safe temporary file path to create.

        Returns:
            ``None`` after the blob is durably closed.
        """

        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self._run("cat-file", "blob", f"{commit}:{path}")
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise PlanError(f"cannot stage committed source {path!r}: {exc}") from exc

    def blob_size(self, commit: str, path: str) -> int:
        """Return the byte size of one committed blob before staging it.

        Args:
            commit: Frozen deployment commit.
            path: Relative Git path owned by the source plan.

        Returns:
            Exact blob size reported by Git.
        """

        raw = self._run("cat-file", "-s", f"{commit}:{path}").decode().strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise PlanError(
                f"Git returned an invalid blob size for {path!r}: {raw!r}"
            ) from exc

    def blob_manifest(self, commit: str, path: str) -> ManifestEntry:
        """Return SHA256 and size for one exact committed source blob.

        Args:
            commit: Frozen deployment commit.
            path: Relative Git path owned by the source plan.

        Returns:
            Content identity used by the stable non-Hybrid plan contract.
        """

        data = self._run("cat-file", "blob", f"{commit}:{path}")
        return ManifestEntry(hashlib.sha256(data).hexdigest(), len(data))

    def _run(self, *arguments: str) -> bytes:
        """Run Git with byte-safe output and convert failures to plan errors."""

        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise PlanError(f"cannot execute Git: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise PlanError(f"Git command failed ({' '.join(arguments)}): {detail}")
        return result.stdout
