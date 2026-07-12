"""Unified source/artifact owner conflict tests."""

from __future__ import annotations

import pytest

from git_deploy.artifact_planner import ArtifactPlan
from git_deploy.combined_planner import CombinedPlanner
from git_deploy.errors import PolicyError
from git_deploy.expected_state import FileEntry
from git_deploy.models import PlannedFile
from git_deploy.state_planner import SourceDiffPlan


def _source(*files: PlannedFile) -> SourceDiffPlan:
    """Return a compact source plan fixture."""

    return SourceDiffPlan("before", "after", files, (), ("t1",), ("t0", "t1"))


def _upload(path: str, digest: str = "new") -> PlannedFile:
    """Return one source/artifact upload operation."""

    return PlannedFile(
        "upload",
        path,
        f"/srv/{path}",
        path,
        None,
        digest,
        3,
    )


def _artifacts(
    files: tuple[PlannedFile, ...],
    entries: tuple[FileEntry, ...],
) -> ArtifactPlan:
    """Return a ready artifact plan fixture."""

    return ArtifactPlan("ready", files, entries, ())


def test_combined_plan_preserves_unmodified_source_and_replaces_artifacts() -> None:
    """Unified target contains unchanged/new source plus authoritative artifacts."""

    current = (
        FileEntry("keep.txt", "source", "keep"),
        FileEntry("old.txt", "source", "old"),
        FileEntry("public/old.js", "artifact:public", "old-artifact"),
    )
    source = _source(
        PlannedFile("delete", "old.txt", "/srv/old.txt", None, "old", None),
        _upload("new.txt"),
    )
    artifact = _artifacts(
        (_upload("public/app.js", "artifact-new"),),
        (FileEntry("public/app.js", "artifact:public", "artifact-new"),),
    )
    combined = CombinedPlanner().combine(current, source, artifact)
    target = {entry.path: entry for entry in combined.target_entries}
    assert set(target) == {"keep.txt", "new.txt", "public/app.js"}
    assert target["new.txt"].owner == "source"
    assert target["public/app.js"].owner == "artifact:public"
    assert [item.path for item in combined.files] == ["new.txt", "old.txt", "public/app.js"]


def test_combined_plan_rejects_source_artifact_exact_conflict() -> None:
    """Source and artifact cannot own or mutate the same destination."""

    source = _source(_upload("public/app.js"))
    artifact = _artifacts(
        (_upload("public/app.js", "artifact"),),
        (FileEntry("public/app.js", "artifact:public", "artifact"),),
    )
    with pytest.raises(PolicyError, match="source/artifact|mutates path twice"):
        CombinedPlanner().combine((), source, artifact)


def test_combined_plan_rejects_owner_hierarchy_conflict() -> None:
    """A managed file cannot be an ancestor of another owner's destination."""

    source = _source(_upload("public"))
    artifact = _artifacts(
        (_upload("public/app.js", "artifact"),),
        (FileEntry("public/app.js", "artifact:public", "artifact"),),
    )
    with pytest.raises(PolicyError, match="hierarchy conflict"):
        CombinedPlanner().combine((), source, artifact)


def test_combined_plan_rejects_artifact_artifact_duplicate() -> None:
    """Two artifact mappings cannot silently collapse the same target file."""

    entries = (
        FileEntry("dist/app.js", "artifact:one", "one"),
        FileEntry("dist/app.js", "artifact:two", "two"),
    )
    with pytest.raises(PolicyError, match="artifact/artifact"):
        CombinedPlanner().combine((), _source(), _artifacts((), entries))


def test_combined_plan_refuses_baseline_required_before_remote() -> None:
    """An untrusted artifact baseline cannot produce a combined mutation plan."""

    with pytest.raises(PolicyError, match="baseline"):
        CombinedPlanner().combine(
            (),
            _source(),
            ArtifactPlan("baseline_required", (), (), (), "baseline required"),
        )
