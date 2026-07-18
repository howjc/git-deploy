#!/usr/bin/env python3
"""Safely aggregate explicit frontend build roots for one Hybrid Output."""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

# Edit these explicit project-relative inputs; order never implies overwrite.
SOURCES = (Path("frontend/dist"), Path("admin/dist"))
DESTINATION = Path(".deploy/frontend-root")


class AggregationError(RuntimeError):
    """Report a deterministic local aggregation safety failure."""


def aggregate(sources: tuple[Path, ...], destination: Path) -> None:
    """Merge explicit roots into one atomically replaced deployment view.

    Args:
        sources: Existing source directories relative to the current project.
        destination: Project-local aggregation directory to replace.

    Returns:
        ``None`` after a complete conflict-free local swap.
    """

    project = Path.cwd().resolve()
    target = _inside_project(destination, project, "destination")
    if target == project:
        raise AggregationError("destination must not be the project root")
    resolved_sources = tuple(
        _inside_project(source, project, f"source {source}") for source in sources
    )
    if not resolved_sources:
        raise AggregationError("at least one explicit source is required")
    if any(
        source == target or source in target.parents or target in source.parents
        for source in resolved_sources
    ):
        raise AggregationError("sources and destination must not overlap")
    for index, source in enumerate(resolved_sources):
        if any(
            source == other or source in other.parents or other in source.parents
            for other in resolved_sources[index + 1 :]
        ):
            raise AggregationError("explicit sources must be unique and non-overlapping")
    if target.exists() and not target.is_dir():
        raise AggregationError("destination must be a directory when it already exists")
    stage = target.parent / f".{target.name}.{uuid.uuid4().hex}.stage"
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.backup"
    stage.mkdir(parents=True, exist_ok=False)
    seen: dict[str, str] = {}
    try:
        for source in resolved_sources:
            _merge_source(source, stage, seen)
        had_target = target.exists() or target.is_symlink()
        if target.is_symlink():
            raise AggregationError("destination must not be a symlink")
        if had_target:
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except BaseException:
            if had_target:
                os.replace(backup, target)
            raise
        if had_target:
            shutil.rmtree(backup)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def _merge_source(source: Path, stage: Path, seen: dict[str, str]) -> None:
    """Copy one verified tree while rejecting duplicate and type conflicts."""

    if source.is_symlink() or not source.is_dir():
        raise AggregationError(f"source must be a regular directory: {source}")

    def visit(directory: Path) -> None:
        """Visit one directory without following symbolic links."""

        for child in sorted(directory.iterdir(), key=lambda path: path.name):
            mode = child.lstat().st_mode
            relative = child.relative_to(source).as_posix()
            _validate_relative(relative)
            if stat.S_ISLNK(mode):
                raise AggregationError(f"symbolic links are not supported: {child}")
            if stat.S_ISDIR(mode):
                _claim(relative, "directory", seen)
                (stage / relative).mkdir(parents=True, exist_ok=True)
                visit(child)
            elif stat.S_ISREG(mode):
                _claim(relative, "file", seen)
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(child, destination)
            else:
                raise AggregationError(f"unsupported file type: {child}")

    visit(source)


def _claim(path: str, kind: str, seen: dict[str, str]) -> None:
    """Claim one relative file/directory and reject ambiguous merge ownership."""

    prior = seen.get(path)
    if prior == "file" or (prior is not None and prior != kind):
        raise AggregationError(f"duplicate or file/directory conflict at {path!r}")
    candidate = PurePosixPath(path)
    for parent in candidate.parents:
        if parent.as_posix() != "." and seen.get(parent.as_posix()) == "file":
            raise AggregationError(f"file/directory conflict at {path!r}")
    if kind == "file" and any(
        PurePosixPath(existing).is_relative_to(candidate)
        for existing in seen
        if existing != path
    ):
        raise AggregationError(f"file/directory conflict at {path!r}")
    seen[path] = kind


def _inside_project(path: Path, project: Path, label: str) -> Path:
    """Normalize one path and reject escape or symbolic-link components."""

    raw = path if path.is_absolute() else project / path
    candidate = Path(os.path.abspath(raw))
    if not candidate.is_relative_to(project):
        raise AggregationError(f"{label} escapes the project root: {path}")
    current = project
    for component in candidate.relative_to(project).parts:
        current /= component
        if current.is_symlink():
            raise AggregationError(f"{label} contains a symbolic link: {current}")
    return candidate


def _validate_relative(value: str) -> None:
    """Reject protected or unstable paths before they enter the Hybrid view."""

    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.parts[0] in {".git", ".deploy", ".git-deploy"}
        or any(
            not part
            or part != part.strip()
            or any(
                ord(character) < 32
                or character == "\x7f"
                or (character.isspace() and character != " ")
                for character in part
            )
            for part in path.parts
        )
    ):
        raise AggregationError(f"unsafe aggregate path: {value!r}")


def main() -> int:
    """Aggregate configured sources and return a shell-friendly success code."""

    aggregate(SOURCES, DESTINATION)
    print(f"Aggregated {len(SOURCES)} source(s) into {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
