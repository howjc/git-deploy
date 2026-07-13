"""Safe artifact collection from isolated build worktrees."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import PolicyError
from .models import ArtifactConfig


@dataclass(frozen=True)
class ArtifactFile:
    """One collected regular file and its remote-relative destination."""

    owner: str
    source_path: Path
    destination: str
    sha256: str
    size: int
    executable: bool


@dataclass(frozen=True)
class ArtifactManifest:
    """Deterministically ordered files produced by configured mappings."""

    files: tuple[ArtifactFile, ...]


class ArtifactCollector:
    """Collect only regular file/tree outputs without following links."""

    def collect(
        self,
        worktree: Path,
        mappings: tuple[ArtifactConfig, ...],
    ) -> ArtifactManifest:
        """Collect configured outputs and reject escapes or special filesystem nodes.

        Args:
            worktree: Exact isolated build worktree root.
            mappings: Validated file/tree mappings.

        Returns:
            Deterministically sorted artifact manifest.
        """

        root = worktree.resolve()
        files: list[ArtifactFile] = []
        destinations: set[str] = set()
        for mapping in mappings:
            source_rel = _safe_relative(mapping.source, "artifact source")
            destination_rel = _safe_relative(mapping.destination, "artifact destination")
            source = root.joinpath(*source_rel.parts)
            self._assert_contained(root, source)
            try:
                source_stat = source.lstat()
            except FileNotFoundError as exc:
                raise PolicyError(f"artifact source does not exist: {mapping.source}") from exc
            if stat.S_ISLNK(source_stat.st_mode):
                raise PolicyError(f"artifact source cannot be a symlink: {mapping.source}")
            if mapping.kind == "file":
                if not stat.S_ISREG(source_stat.st_mode):
                    raise PolicyError(f"file artifact is not a regular file: {mapping.source}")
                self._append_file(
                    files,
                    destinations,
                    mapping,
                    source,
                    destination_rel.as_posix(),
                )
                continue
            if mapping.kind != "tree" or not stat.S_ISDIR(source_stat.st_mode):
                raise PolicyError(f"tree artifact is not a directory: {mapping.source}")
            if (source / ".git").exists() or (source / ".git").is_symlink():
                raise PolicyError(f"artifact tree cannot be a submodule/worktree: {mapping.source}")
            for current, dir_names, file_names in os.walk(source, topdown=True, followlinks=False):
                current_path = Path(current)
                for directory in tuple(dir_names):
                    path = current_path / directory
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        raise PolicyError(f"artifact tree contains symlink: {path.relative_to(root)}")
                    if not stat.S_ISDIR(mode):
                        raise PolicyError(f"artifact tree contains special node: {path.relative_to(root)}")
                    if directory == ".git":
                        raise PolicyError(
                            f"artifact tree contains submodule/worktree metadata: {path.relative_to(root)}"
                        )
                for filename in file_names:
                    path = current_path / filename
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        raise PolicyError(f"artifact tree contains symlink: {path.relative_to(root)}")
                    if not stat.S_ISREG(mode):
                        raise PolicyError(
                            f"artifact tree contains non-regular file: {path.relative_to(root)}"
                        )
                    relative = path.relative_to(source).as_posix()
                    destination = (destination_rel / relative).as_posix()
                    self._append_file(files, destinations, mapping, path, destination)
        return ArtifactManifest(files=tuple(sorted(files, key=lambda item: item.destination)))

    @staticmethod
    def _assert_contained(root: Path, source: Path) -> None:
        """Reject paths that escape after normalization without following contents."""

        try:
            source.relative_to(root)
        except ValueError as exc:
            raise PolicyError(f"artifact source escapes worktree: {source}") from exc

    @staticmethod
    def _append_file(
        files: list[ArtifactFile],
        destinations: set[str],
        mapping: ArtifactConfig,
        source: Path,
        destination: str,
    ) -> None:
        """Hash and append one regular file while enforcing unique destinations."""

        if destination in destinations:
            raise PolicyError(f"artifact destination conflict: {destination}")
        destinations.add(destination)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        mode = source.stat(follow_symlinks=False).st_mode
        files.append(
            ArtifactFile(
                owner=mapping.owner,
                source_path=source,
                destination=destination,
                sha256=digest.hexdigest(),
                size=size,
                executable=bool(mode & 0o111),
            )
        )


def _safe_relative(value: str, field: str) -> PurePosixPath:
    """Defensively validate a non-empty POSIX-relative artifact path."""

    if not value or value.startswith("/") or "\\" in value:
        raise PolicyError(f"{field} must be a POSIX-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyError(f"{field} contains traversal or empty segments")
    return PurePosixPath(value)
