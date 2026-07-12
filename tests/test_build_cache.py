"""Build fingerprint and CAS-backed artifact cache tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from git_deploy.artifacts import ArtifactCollector
from git_deploy.build_cache import BuildCache, build_fingerprint, docker_runner_identity
from git_deploy.models import (
    ArtifactConfig,
    BuildConfig,
    DockerBuildConfig,
    OnePasswordConfig,
    ProjectConfig,
)
from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity


def _inputs(tmp_path: Path):
    """Return one host build, mapping, and collected artifact manifest."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "dist").write_bytes(b"artifact")
    build = BuildConfig(
        runner="host",
        commands=(("tool", "build"),),
        timeout=60,
        cwd=".",
        env_allowlist=("CI",),
    )
    artifacts = (ArtifactConfig(source="dist", destination="dist", kind="file"),)
    manifest = ArtifactCollector().collect(worktree, artifacts)
    return build, artifacts, manifest


def test_core_fingerprint_changes_for_every_reproducibility_input(tmp_path: Path) -> None:
    """Tree/commands/cwd/timeout/lock/tool/mapping/runner identity each cause a miss."""

    build, artifacts, _manifest = _inputs(tmp_path)

    def fingerprint(
        *,
        tree: str = "tree-a",
        selected_build: BuildConfig = build,
        selected_artifacts=artifacts,
        tools=None,
        locks=None,
        runner=None,
    ) -> str:
        return build_fingerprint(
            source_tree_id=tree,
            build=selected_build,
            artifacts=selected_artifacts,
            tool_versions=tools or {"tool": "1"},
            lock_digests=locks or {"lock": "aaa"},
            runner_identity=runner or {"host": "linux"},
        )

    baseline = fingerprint()
    variants = {
        fingerprint(tree="tree-b"),
        fingerprint(selected_build=replace(build, commands=(("tool", "other"),))),
        fingerprint(selected_build=replace(build, cwd="subdir")),
        fingerprint(selected_build=replace(build, timeout=61)),
        fingerprint(locks={"lock": "bbb"}),
        fingerprint(tools={"tool": "2"}),
        fingerprint(
            selected_artifacts=(ArtifactConfig("dist", "other", "file"),)
        ),
        fingerprint(runner={"host": "other-platform"}),
    }
    assert baseline not in variants
    assert len(variants) == 8
    assert fingerprint() == baseline


def test_core_cache_identical_hit_and_changed_fingerprint_miss(tmp_path: Path) -> None:
    """A stored manifest reopens from CAS; a different fingerprint is a clean miss."""

    build, artifacts, manifest = _inputs(tmp_path)
    fingerprint = build_fingerprint(
        source_tree_id="tree-a",
        build=build,
        artifacts=artifacts,
    )
    cache = BuildCache(tmp_path / "target")
    assert cache.lookup(fingerprint).hit is False
    stored = cache.store(fingerprint, "tree-a", manifest)
    assert stored is not None
    hit = BuildCache(tmp_path / "target").lookup(fingerprint)
    assert hit.hit is True and hit.entry is not None
    assert hit.entry.artifacts[0].content_sha256 == hashlib.sha256(b"artifact").hexdigest()
    assert cache.lookup("0" * 64).hit is False


def test_core_build_fingerprint_does_not_change_target_or_policy(tmp_path: Path) -> None:
    """Build commands are cache inputs, never physical/managed-state identity inputs."""

    repository = tmp_path / "repo"
    repository.mkdir()
    first = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv",
        build=BuildConfig(runner="host", commands=(("one",),)),
    )
    second = replace(
        first,
        build=BuildConfig(runner="host", commands=(("two",),)),
    )
    server = {"protocol": "sftp", "host": "example"}
    assert resolve_target_identity(server, first).target_id == resolve_target_identity(server, second).target_id
    assert policy_fingerprint_for_project(first) == policy_fingerprint_for_project(second)


def test_core_artifact_destination_changes_managed_policy(tmp_path: Path) -> None:
    """Artifact destinations alter managed policy even though build commands do not."""

    repository = tmp_path / "repo"
    repository.mkdir()
    first = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv",
        artifacts=(ArtifactConfig("dist", "public", "tree"),),
    )
    second = replace(
        first,
        artifacts=(ArtifactConfig("dist", "preview", "tree"),),
    )
    assert policy_fingerprint_for_project(first) != policy_fingerprint_for_project(second)


def test_docker_fingerprint_conformance() -> None:
    """Image/platform/network/pull/UID/GID identities each invalidate Docker cache."""

    build = BuildConfig(
        runner="docker",
        commands=(("tool",),),
        docker=DockerBuildConfig("image", "linux/amd64", "none", "never"),
    )

    def fingerprint(selected: BuildConfig, image_id: str = "sha256:one") -> str:
        return build_fingerprint(
            source_tree_id="tree",
            build=selected,
            artifacts=(),
            runner_identity=docker_runner_identity(selected, image_id),
        )

    baseline = fingerprint(build)
    assert build.docker is not None
    docker = build.docker
    assert fingerprint(build, "sha256:two") != baseline
    assert fingerprint(replace(build, docker=replace(docker, platform="linux/arm64"))) != baseline
    assert fingerprint(replace(build, docker=replace(docker, network="bridge"))) != baseline
    assert fingerprint(replace(build, docker=replace(docker, pull_policy="missing"))) != baseline
    identity = docker_runner_identity(build, "sha256:one")
    identity["uid"] = int(identity["uid"]) + 1
    assert build_fingerprint(
        source_tree_id="tree", build=build, artifacts=(), runner_identity=identity
    ) != baseline
    identity = docker_runner_identity(build, "sha256:one")
    identity["gid"] = int(identity["gid"]) + 1
    assert build_fingerprint(
        source_tree_id="tree", build=build, artifacts=(), runner_identity=identity
    ) != baseline


def test_onepassword_cache_bypass_and_reference_omission(tmp_path: Path) -> None:
    """Secret builds stage bytes but never publish a reusable cache manifest or URI."""

    build, artifacts, manifest = _inputs(tmp_path)
    secret_build = replace(
        build,
        env_allowlist=("TOKEN",),
        onepassword=OnePasswordConfig((("TOKEN", "op://vault/item/token"),)),
    )
    fingerprint = build_fingerprint(
        source_tree_id="tree",
        build=secret_build,
        artifacts=artifacts,
    )
    cache = BuildCache(tmp_path / "target")
    assert cache.lookup(fingerprint, secrets_enabled=True).reason.startswith("1password")
    entry = cache.store(
        fingerprint,
        "tree",
        manifest,
        secrets_enabled=True,
    )
    assert entry is not None
    assert not (cache.root / f"{fingerprint}.json").exists()
    assert cache.lookup(fingerprint).hit is False
    assert "op://" not in fingerprint
    state_bytes = "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / "target").rglob("*.json")
    )
    assert "op://" not in state_bytes
