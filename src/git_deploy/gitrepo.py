"""Read commit ranges and file bytes directly from Git objects."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ConfigurationError, PolicyError
from .models import DeploymentPlan, GitChange, PlannedFile, ProjectConfig


@dataclass(frozen=True)
class GitBlob:
    """Blob metadata and bytes stored at one commit path."""

    path: str
    mode: str
    data: bytes

    @property
    def sha256(self) -> str:
        """Return a content SHA-256 used for remote drift checks."""

        return hashlib.sha256(self.data).hexdigest()

    @property
    def executable(self) -> bool:
        """Return whether the Git tree records an executable regular file."""

        return self.mode == "100755"


class GitRepository:
    """Safe subprocess adapter for a local Git repository."""

    def __init__(self, path: Path):
        """Validate and retain a Git working tree.

        Args:
            path: Repository working tree path.
        """

        self.path = path
        top = self._run_text("rev-parse", "--show-toplevel").strip()
        if Path(top).resolve() != path.resolve():
            raise ConfigurationError(
                f"configured repository must be its Git root: {path} (actual root: {top})"
            )

    def resolve_commit(self, revision: str) -> str:
        """Resolve a revision to a full commit object ID.

        Args:
            revision: User-provided commit, tag, or branch expression.

        Returns:
            Full hexadecimal commit ID.
        """

        if not revision.strip():
            raise ConfigurationError("empty Git revision")
        return self._run_text("rev-parse", "--verify", f"{revision}^{{commit}}").strip()

    def require_ancestor(self, older: str, newer: str) -> None:
        """Require a forward commit range by default.

        Args:
            older: Resolved source commit.
            newer: Resolved target commit.
        """

        proc = self._run("merge-base", "--is-ancestor", older, newer, check=False)
        if proc.returncode != 0:
            raise ConfigurationError(
                f"source commit {older[:12]} is not an ancestor of target {newer[:12]}"
            )

    def changes(self, older: str, newer: str) -> tuple[GitChange, ...]:
        """Parse a NUL-delimited Git name-status diff.

        Args:
            older: Resolved source commit.
            newer: Resolved target commit.

        Returns:
            Ordered normalized changes with rename/copy metadata.
        """

        output = self._run(
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies",
            "--find-copies-harder",
            older,
            newer,
            "--",
        ).stdout
        return _parse_name_status(output)

    def working_tree_changes(self) -> tuple[GitChange, ...]:
        """Return tracked and untracked changes omitted from commit deployments.

        Returns:
            Net changes between ``HEAD`` and the current working tree plus untracked files.
        """

        tracked = _parse_name_status(
            self._run(
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                "--find-copies",
                "HEAD",
                "--",
            ).stdout
        )
        untracked_output = self._run(
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
        untracked = tuple(
            GitChange(status="?", path=raw.decode("utf-8", errors="surrogateescape"))
            for raw in untracked_output.split(b"\0")
            if raw
        )
        return (*tracked, *untracked)
    def blob(self, commit: str, path: str) -> GitBlob | None:
        """Read a regular file exactly as stored in a commit.

        Args:
            commit: Resolved commit ID.
            path: Repository-relative POSIX path.

        Returns:
            Blob metadata, or ``None`` when the path is absent.
        """

        _validate_repo_path(path)
        tree = self._run("ls-tree", "-z", commit, "--", path).stdout
        if not tree:
            return None
        header, separator, listed_path = tree.partition(b"\t")
        if not separator or listed_path.rstrip(b"\0").decode("utf-8", errors="surrogateescape") != path:
            return None
        parts = header.decode("ascii").split()
        if len(parts) != 3:
            raise ConfigurationError(f"unexpected git tree entry for {path}")
        mode, object_type, object_id = parts
        if object_type == "commit" or mode == "160000":
            raise PolicyError(f"Git submodule changes are not supported: {path}")
        if mode == "120000":
            raise PolicyError(f"Git symlink changes are not supported: {path}")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise PolicyError(f"unsupported Git object mode {mode} for {path}")
        data = self._run("cat-file", "blob", object_id).stdout
        return GitBlob(path=path, mode=mode, data=data)

    def _run_text(self, *args: str) -> str:
        """Run Git and decode standard output as UTF-8 text.

        Args:
            args: Arguments following the ``git`` executable.

        Returns:
            Decoded standard output.
        """

        return self._run(*args).stdout.decode("utf-8", errors="replace")

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Run a Git subprocess without invoking a shell.

        Args:
            args: Arguments following the ``git`` executable.
            check: Raise a configuration error for non-zero exit status.

        Returns:
            Completed binary subprocess result.
        """

        try:
            proc = subprocess.run(
                ["git", "-C", str(self.path), *args],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConfigurationError("git executable not found") from exc
        if check and proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ConfigurationError(f"git {' '.join(args)} failed: {detail}")
        return proc


def _parse_name_status(output: bytes) -> tuple[GitChange, ...]:
    """Parse NUL-delimited output from ``git diff --name-status``.

    Args:
        output: Raw Git subprocess output.

    Returns:
        Ordered normalized changes with rename/copy metadata.
    """

    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()

    result: list[GitChange] = []
    index = 0
    while index < len(fields):
        token = fields[index].decode("ascii", errors="strict")
        index += 1
        kind = token[:1]
        score = int(token[1:]) if token[1:].isdigit() else None
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise ConfigurationError("malformed rename/copy record from git diff")
            old_path = fields[index].decode("utf-8", errors="surrogateescape")
            path = fields[index + 1].decode("utf-8", errors="surrogateescape")
            index += 2
            result.append(GitChange(status=kind, path=path, old_path=old_path, score=score))
            continue
        if index >= len(fields):
            raise ConfigurationError("malformed path record from git diff")
        path = fields[index].decode("utf-8", errors="surrogateescape")
        index += 1
        result.append(GitChange(status=kind, path=path, score=score))
    return tuple(result)


class GitDeploymentPlanner:
    """Convert selected Git changes into remote file mutations."""

    ALWAYS_PROTECTED = (
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        "*.pem",
        "**/*.pem",
        "*.key",
        "**/*.key",
        "*.p12",
        "**/*.p12",
        "*.pfx",
        "**/*.pfx",
        ".git/**",
        "deploy.toml",
        "**/deploy.toml",
    )

    def __init__(self, project: ProjectConfig):
        """Bind planning policy to one configured project.

        Args:
            project: Resolved project configuration.
        """

        self.project = project
        self.repository = GitRepository(project.repository)

    def build(self, from_revision: str, to_revision: str) -> DeploymentPlan:
        """Build a deterministic deployment plan between two revisions.

        Args:
            from_revision: Commit currently expected on the server.
            to_revision: Commit whose tracked bytes should be deployed.

        Returns:
            Filtered immutable file operation plan.
        """

        older = self.repository.resolve_commit(from_revision)
        newer = self.repository.resolve_commit(to_revision)
        self.repository.require_ancestor(older, newer)
        operations: list[PlannedFile] = []
        excluded: list[GitChange] = []

        for change in self.repository.changes(older, newer):
            selected_paths = [change.path]
            if change.old_path:
                selected_paths.append(change.old_path)
            if not any(self._selected(path) for path in selected_paths):
                excluded.append(change)
                continue
            for path in selected_paths:
                if self._selected(path):
                    self._assert_not_protected(path)
            operations.extend(self._operations(change, older, newer))

        return DeploymentPlan(
            project=self.project.name,
            repository=self.project.repository,
            remote_root=self.project.remote_root,
            from_commit=older,
            to_commit=newer,
            files=tuple(operations),
            excluded=tuple(excluded),
        )

    def target_bytes(self, plan: DeploymentPlan, operation: PlannedFile) -> bytes:
        """Read target bytes for one upload operation.

        Args:
            plan: Plan containing the target commit.
            operation: Upload operation with a source path.

        Returns:
            Exact bytes stored in the target Git commit.
        """

        if operation.source_path is None:
            raise ConfigurationError(f"operation {operation.action} has no source path")
        blob = self.repository.blob(plan.to_commit, operation.source_path)
        if blob is None:
            raise ConfigurationError(f"target blob disappeared: {operation.source_path}")
        return blob.data

    def _operations(self, change: GitChange, older: str, newer: str) -> list[PlannedFile]:
        """Expand one Git status into upload/delete operations.

        Args:
            change: Parsed Git name-status record.
            older: Resolved source commit.
            newer: Resolved target commit.

        Returns:
            One or two remote file operations.
        """

        kind = change.status
        if kind in {"A", "M"}:
            return [self._upload(change.path, change.path, older, newer)]
        if kind == "D":
            return [self._delete(change.path, older)]
        if kind == "R":
            assert change.old_path is not None
            result: list[PlannedFile] = []
            if self._selected(change.path):
                result.append(self._upload(change.path, change.path, older, newer))
            if self._selected(change.old_path):
                result.append(self._delete(change.old_path, older))
            return result
        if kind == "C":
            if not self._selected(change.path):
                return []
            return [self._upload(change.path, change.path, older, newer)]
        raise PolicyError(f"unsupported Git change status {kind} for {change.path}")

    def _upload(self, path: str, source_path: str, older: str, newer: str) -> PlannedFile:
        """Create an upload operation with source and target hashes.

        Args:
            path: Destination repository-relative path.
            source_path: Target commit path supplying bytes.
            older: Source commit.
            newer: Target commit.

        Returns:
            Planned upload operation.
        """

        target = self.repository.blob(newer, source_path)
        if target is None:
            raise ConfigurationError(f"missing target file {source_path}")
        before = self.repository.blob(older, path)
        return PlannedFile(
            action="upload",
            path=path,
            remote_path=_remote_path(self.project.remote_root, path),
            source_path=source_path,
            expected_before_sha256=before.sha256 if before else None,
            target_sha256=target.sha256,
            target_size=len(target.data),
            executable=target.executable,
            expected_before_executable=before.executable if before else None,
        )

    def _delete(self, path: str, older: str) -> PlannedFile:
        """Create a remote delete operation with a baseline hash.

        Args:
            path: Repository-relative path removed by the target commit.
            older: Source commit.

        Returns:
            Planned delete operation.
        """

        before = self.repository.blob(older, path)
        if before is None:
            raise ConfigurationError(f"deleted source file is absent from source commit: {path}")
        return PlannedFile(
            action="delete",
            path=path,
            remote_path=_remote_path(self.project.remote_root, path),
            source_path=None,
            expected_before_sha256=before.sha256,
            target_sha256=None,
            expected_before_executable=before.executable,
        )

    def _selected(self, path: str) -> bool:
        """Return whether include/exclude rules select a repository path.

        Args:
            path: Repository-relative POSIX path.

        Returns:
            ``True`` when at least one include and no exclude matches.
        """

        _validate_repo_path(path)
        included = any(_match(path, pattern) for pattern in self.project.include)
        excluded = any(_match(path, pattern) for pattern in self.project.exclude)
        return included and not excluded

    def _assert_not_protected(self, path: str) -> None:
        """Block deployment of built-in or configured sensitive paths.

        Args:
            path: Selected repository-relative path.
        """

        patterns = (*self.ALWAYS_PROTECTED, *self.project.protected)
        if any(_match(path, pattern) for pattern in patterns):
            raise PolicyError(f"protected path cannot be deployed: {path}")


def _validate_repo_path(path: str) -> None:
    """Reject absolute and parent-traversing repository paths.

    Args:
        path: Candidate Git path.
    """

    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise PolicyError(f"unsafe repository path: {path!r}")


def _remote_path(remote_root: str, path: str) -> str:
    """Join one validated Git path below an absolute remote root.

    Args:
        remote_root: Configured absolute remote directory.
        path: Validated repository-relative POSIX path.

    Returns:
        Absolute remote path.
    """

    _validate_repo_path(path)
    return remote_root.rstrip("/") + "/" + path


def _match(path: str, pattern: str) -> bool:
    """Match a slash-separated path using deployment glob conventions.

    Args:
        path: Repository-relative path.
        pattern: Configured glob; a trailing ``/**`` includes the directory tree.

    Returns:
        Whether the pattern selects the path.
    """

    from fnmatch import fnmatchcase

    if pattern == "**":
        return True
    if pattern.endswith("/**") and path == pattern[:-3].rstrip("/"):
        return True
    return fnmatchcase(path, pattern)
