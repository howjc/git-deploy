"""Trusted artifact diff and baseline planner tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_deploy.artifact_planner import ArtifactPlanner
from git_deploy.build_cache import BuildCacheEntry, CachedArtifact
from git_deploy.errors import PolicyError
from git_deploy.expected_state import FileEntry, build_expected_state
from git_deploy.models import ArtifactConfig, ProjectConfig
from git_deploy.state_executor import FakeRemotePath, InMemoryTransport


def _project(tmp_path: Path) -> ProjectConfig:
    """Return a project with file and tree artifact mappings."""

    return ProjectConfig(
        name="demo",
        repository=tmp_path,
        remote_root="/srv",
        artifacts=(
            ArtifactConfig("server", "bin/server", "file"),
            ArtifactConfig("dist", "public", "tree"),
        ),
    )


def _state(*files: FileEntry, trusted: bool = True):
    """Build a minimal expected state with optional artifact provenance."""

    return build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="tree",
        applied_transition_ids=(),
        physical_fingerprint="physical",
        policy_fingerprint="policy",
        files=files,
        artifacts=({"build_fingerprint": "old"},) if trusted else (),
    )


def test_trusted_diff_covers_add_modify_delete_mode_and_content_refs(tmp_path: Path) -> None:
    """Trusted current manifest determines all artifact mutations and target owners."""

    old_a = hashlib.sha256(b"old-a").hexdigest()
    new_a = hashlib.sha256(b"new-a").hexdigest()
    removed = hashlib.sha256(b"removed").hexdigest()
    added = hashlib.sha256(b"added").hexdigest()
    current = _state(
        FileEntry("bin/server", "artifact:bin/server", old_a, executable=False),
        FileEntry("public/removed.js", "artifact:public", removed),
    )
    target = BuildCacheEntry(
        "fingerprint",
        "tree-target",
        (
            CachedArtifact("artifact:bin/server", "bin/server", new_a, 5, True),
            CachedArtifact("artifact:public", "public/added.js", added, 5, False),
        ),
    )
    plan = ArtifactPlanner().plan(_project(tmp_path), current, target)
    assert plan.status == "ready"
    by_path = {item.path: item for item in plan.files}
    assert by_path["bin/server"].action == "upload"
    assert by_path["bin/server"].expected_before_sha256 == old_a
    assert by_path["bin/server"].executable is True
    assert by_path["public/removed.js"].action == "delete"
    assert by_path["public/added.js"].action == "upload"
    assert dict(plan.content_refs)["public/added.js"] == added
    assert {entry.owner for entry in plan.target_entries} == {
        "artifact:bin/server",
        "artifact:public",
    }


def test_trusted_diff_missing_manifest_requires_explicit_baseline(tmp_path: Path) -> None:
    """No artifact provenance never means remote-empty and performs no remote reads."""

    target = BuildCacheEntry("fingerprint", "tree", ())
    plan = ArtifactPlanner().plan(_project(tmp_path), _state(trusted=False), target)
    assert plan.status == "baseline_required"
    assert plan.files == ()


@pytest.mark.parametrize("mode", ["upload", "delete"])
def test_artifact_upload_and_delete_cannot_cross_protected_paths(
    tmp_path: Path,
    mode: str,
) -> None:
    """Apply built-in protected policy to artifact uploads and deletions."""

    digest = hashlib.sha256(b"secret").hexdigest()
    current = _state(
        *(
            (FileEntry(".env", "artifact:.env", digest),)
            if mode == "delete"
            else ()
        )
    )
    target = BuildCacheEntry(
        "fingerprint",
        "tree",
        (
            (CachedArtifact("artifact:.env", ".env", digest, 6, False),)
            if mode == "upload"
            else ()
        ),
    )

    with pytest.raises(PolicyError, match="protected path"):
        ArtifactPlanner().plan(_project(tmp_path), current, target)


def test_baseline_known_source_requires_exact_remote_bytes(tmp_path: Path) -> None:
    """Known-source baseline adopts only reproducible bytes with zero writes."""

    digest = hashlib.sha256(b"server").hexdigest()
    baseline = BuildCacheEntry(
        "fingerprint",
        "known-tree",
        (CachedArtifact("artifact:bin/server", "bin/server", digest, 6, True),),
    )
    transport = InMemoryTransport()
    transport.files["/srv/bin/server"] = FakeRemotePath(b"server", executable=True)
    result = ArtifactPlanner().verify_known_source_baseline(
        _project(tmp_path), baseline, transport
    )
    assert result.files[0].content_sha256 == digest
    assert result.provenance[0]["source_tree_id"] == "known-tree"
    assert transport.write_calls == 0

    transport.files["/srv/bin/server"] = FakeRemotePath(b"unknown")
    with pytest.raises(PolicyError, match="known-source"):
        ArtifactPlanner().verify_known_source_baseline(
            _project(tmp_path), baseline, transport
        )
    assert transport.write_calls == 0


def test_baseline_empty_verifies_all_file_and_tree_destinations(tmp_path: Path) -> None:
    """Explicit empty succeeds only when no configured destination path exists."""

    project = _project(tmp_path)
    transport = InMemoryTransport()
    result = ArtifactPlanner().verify_empty_baseline(project, transport)
    assert result.files == ()
    assert result.provenance == ({"mode": "empty"},)
    assert transport.write_calls == 0

    transport.files["/srv/public/existing.js"] = FakeRemotePath(b"unknown")
    with pytest.raises(PolicyError, match="destination exists"):
        ArtifactPlanner().verify_empty_baseline(project, transport)
    assert transport.write_calls == 0
