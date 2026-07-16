"""Scan build outputs and persist the small per-target success state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from git_deploy.config import OutputConfig
from git_deploy.errors import PlanError, StateError


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """Record the content identity and size of one output file."""

    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ScannedOutput:
    """Bind one current output manifest record to its local file."""

    local_path: Path
    entry: ManifestEntry


@dataclass(frozen=True, slots=True)
class TargetState:
    """Represent the complete lightweight state for one target."""

    schema: int
    target: str
    target_fingerprint: str
    last_commit: str
    deployed_at: int
    outputs: dict[str, ManifestEntry]


class StateStore:
    """Read and atomically commit target state below the shared Git common dir."""

    def __init__(self, git_dir: Path) -> None:
        """Bind state storage to a Git metadata directory.

        Args:
            git_dir: Result of ``git rev-parse --git-common-dir``.
        """

        self.base = git_dir / "git-deploy"

    def path_for(self, target: str) -> Path:
        """Return the isolated state path for a validated target name.

        Args:
            target: Configuration target name.

        Returns:
            Path below ``.git/git-deploy``.
        """

        if not target or target in {".", ".."} or "/" in target or "\\" in target:
            raise StateError(f"unsafe target state name: {target!r}")
        return self.base / f"{target}.json"

    def load(self, target: str) -> TargetState | None:
        """Load and validate state, returning ``None`` for a first deployment.

        Args:
            target: Selected target name.

        Returns:
            Valid state or ``None`` when it has never been committed.
        """

        path = self.path_for(target)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError(f"cannot read state {path}: {exc}; use --full after repairing/removing it") from exc
        return _parse_state(raw, target, path)

    def save(self, state: TargetState) -> None:
        """Atomically replace state only after all remote operations succeed.

        Args:
            state: Complete new target state.

        Returns:
            ``None`` after atomic replacement and directory sync.
        """

        path = self.path_for(state.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": state.schema,
            "target": state.target,
            "target_fingerprint": state.target_fingerprint,
            "last_commit": state.last_commit,
            "deployed_at": state.deployed_at,
            "outputs": {key: asdict(value) for key, value in sorted(state.outputs.items())},
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise StateError(f"cannot atomically write state {path}: {exc}") from exc

    def migrate_from(self, legacy: StateStore, target: str) -> bool:
        """Copy a valid per-worktree state into common storage when still absent.

        Args:
            legacy: Store rooted at the old ``git rev-parse --git-dir`` path.
            target: Selected target name.

        Returns:
            ``True`` only when a legacy state was copied. The old file is retained
            so downgrade or interrupted migration cannot lose deployment evidence.
        """

        if legacy.base == self.base or self.path_for(target).exists():
            return False
        state = legacy.load(target)
        if state is None:
            return False
        self.save(state)
        return True


def scan_outputs(outputs: tuple[OutputConfig, ...]) -> dict[str, ScannedOutput]:
    """Hash all regular output files and map them to remote relative paths.

    Args:
        outputs: Validated local-to-remote output mappings.

    Returns:
        Mapping from normalized remote path to current local content.
    """

    scanned: dict[str, ScannedOutput] = {}
    for output in outputs:
        if not output.local.exists():
            # A missing configured root usually means a broken build or typo. It
            # must not be interpreted as deliberate removal of every old output.
            raise PlanError(f"configured output does not exist after build: {output.local}")
        if output.local.is_symlink():
            raise PlanError(f"output path must not be a symlink: {output.local}")
        if output.local.is_file():
            candidates = ((output.local, PurePosixPath(output.local.name)),)
        elif output.local.is_dir():
            candidates = tuple(
                (path, PurePosixPath(path.relative_to(output.local).as_posix()))
                for path in sorted(output.local.rglob("*"))
                if path.is_file()
            )
        else:
            raise PlanError(f"output path is not a regular file or directory: {output.local}")
        for local_path, relative in candidates:
            if local_path.is_symlink() or not local_path.resolve().is_relative_to(output.local.resolve()):
                raise PlanError(f"output file must not escape through a symlink: {local_path}")
            remote = (output.remote / relative).as_posix()
            if remote in scanned:
                raise PlanError(f"multiple outputs map to the same remote path: {remote}")
            scanned[remote] = ScannedOutput(local_path, hash_file(local_path))
    return dict(sorted(scanned.items()))


def hash_file(path: Path) -> ManifestEntry:
    """Calculate SHA256 and byte size for one regular local file.

    Args:
        path: Output file to read.

    Returns:
        Immutable content manifest entry.
    """

    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise PlanError(f"cannot hash output file {path}: {exc}") from exc
    return ManifestEntry(digest.hexdigest(), size)


def new_state(
    target: str,
    fingerprint: str,
    head: str,
    outputs: dict[str, ManifestEntry],
) -> TargetState:
    """Create the state committed after a completely successful deployment.

    Args:
        target: Selected target name.
        fingerprint: Non-secret physical target identity.
        head: Exact deployed source commit.
        outputs: Complete current output manifest.

    Returns:
        Schema-1 state stamped with the current Unix time.
    """

    return TargetState(1, target, fingerprint, head, int(time.time()), outputs)


def _parse_state(raw: Any, target: str, path: Path) -> TargetState:
    """Validate untrusted JSON state and build the typed model."""

    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise StateError(f"unsupported or missing state schema in {path}; rerun with --full")
    if raw.get("target") != target:
        raise StateError(f"state target mismatch in {path}; rerun with --full")
    fingerprint = raw.get("target_fingerprint")
    last_commit = raw.get("last_commit")
    deployed_at = raw.get("deployed_at")
    outputs_raw = raw.get("outputs")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise StateError(f"invalid target_fingerprint in {path}")
    if not isinstance(last_commit, str) or not last_commit:
        raise StateError(f"invalid last_commit in {path}")
    if not isinstance(deployed_at, int) or isinstance(deployed_at, bool) or deployed_at < 0:
        raise StateError(f"invalid deployed_at in {path}")
    if not isinstance(outputs_raw, dict):
        raise StateError(f"invalid outputs in {path}")
    outputs: dict[str, ManifestEntry] = {}
    for remote, value in outputs_raw.items():
        if (
            not isinstance(remote, str)
            or not remote
            or PurePosixPath(remote).is_absolute()
            or ".." in PurePosixPath(remote).parts
            or not isinstance(value, dict)
        ):
            raise StateError(f"invalid output record in {path}: {remote!r}")
        sha256 = value.get("sha256")
        size = value.get("size")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise StateError(f"invalid output content record in {path}: {remote!r}")
        outputs[remote] = ManifestEntry(sha256, size)
    return TargetState(1, target, fingerprint, last_commit, deployed_at, outputs)
