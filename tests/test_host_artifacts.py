"""Offline Composer, Node, and Go host artifact integration fixtures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from git_deploy.build_runner import BuildExecutionError
from git_deploy.build_service import BuildService
from git_deploy.models import ArtifactConfig, BuildConfig, ProjectConfig


def _git(repo: Path, *args: str) -> str:
    """Run Git in an integration fixture."""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init(repo: Path) -> None:
    """Initialize one fixture repository."""

    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "T")


def _commit(repo: Path, message: str) -> str:
    """Commit all fixture changes and return the exact tree id."""

    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD^{tree}")


def _composer_lock(repo: Path) -> None:
    """Regenerate a local-path-only Composer lock without network access."""

    environment = os.environ.copy()
    environment["COMPOSER_DISABLE_NETWORK"] = "1"
    subprocess.run(
        [
            shutil.which("composer") or "composer",
            "update",
            "--no-install",
            "--no-interaction",
            "--no-plugins",
            "--no-scripts",
            "--no-progress",
        ],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
    )


def test_composer_offline_vendor_diff_cache_and_failure_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline path package covers vendor add/modify/delete/mode, cache, and cleanup."""

    repo = tmp_path / "repo"
    _init(repo)
    package = repo / "packages/lib"
    (package / "src").mkdir(parents=True)
    (package / "composer.json").write_text(
        json.dumps({"name": "demo/lib", "version": "1.0.0", "autoload": {"psr-4": {"Demo\\": "src/"}}}),
        encoding="utf-8",
    )
    (package / "src/Lib.php").write_text("<?php // one\n", encoding="utf-8")
    (package / "src/Remove.php").write_text("<?php // remove\n", encoding="utf-8")
    tool = package / "bin/tool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    (repo / "composer.json").write_text(
        json.dumps(
            {
                "name": "demo/root",
                "repositories": [
                    {"type": "path", "url": "packages/lib", "options": {"symlink": False}}
                ],
                "require": {"demo/lib": "*"},
                "minimum-stability": "dev",
            }
        ),
        encoding="utf-8",
    )
    _composer_lock(repo)
    tree_one = _commit(repo, "composer one")
    (package / "src/Lib.php").write_text("<?php // two\n", encoding="utf-8")
    (package / "src/Remove.php").unlink()
    (package / "src/Add.php").write_text("<?php // add\n", encoding="utf-8")
    _composer_lock(repo)
    tree_two = _commit(repo, "composer two")

    monkeypatch.setenv("COMPOSER_DISABLE_NETWORK", "1")
    build = BuildConfig(
        runner="host",
        commands=((
            shutil.which("composer") or "composer",
            "install",
            "--no-dev",
            "--no-interaction",
            "--no-plugins",
            "--no-scripts",
            "--no-progress",
        ),),
        env_allowlist=("COMPOSER_DISABLE_NETWORK",),
    )
    project = ProjectConfig(
        "demo",
        repo,
        "/srv",
        build=build,
        artifacts=(ArtifactConfig("vendor", "vendor", "tree"),),
    )
    service = BuildService(project, tmp_path / "target")
    first = service.execute(tree_one)
    second = service.execute(tree_two)
    repeated = service.execute(tree_two)
    first_paths = {item.destination for item in first.entry.artifacts}
    second_paths = {item.destination for item in second.entry.artifacts}
    assert "vendor/demo/lib/src/Remove.php" in first_paths
    assert "vendor/demo/lib/src/Remove.php" not in second_paths
    assert "vendor/demo/lib/src/Add.php" in second_paths
    assert first.fingerprint != second.fingerprint
    assert repeated.cache_hit is True
    assert any(
        item.destination == "vendor/demo/lib/bin/tool" and item.executable
        for item in second.entry.artifacts
    )

    (repo / "composer.json").write_text("{invalid", encoding="utf-8")
    bad_tree = _commit(repo, "composer failure")
    with pytest.raises(BuildExecutionError):
        service.execute(bad_tree)
    assert list((tmp_path / "target/build/worktrees").iterdir()) == []


def test_node_offline_dist_artifact_and_cache(tmp_path: Path) -> None:
    """Offline npm ci/build emits deterministic dist files and cache hits."""

    repo = tmp_path / "repo"
    _init(repo)
    (repo / "src.txt").write_text("node-one", encoding="utf-8")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "1.0.0",
                "private": True,
                "scripts": {
                    "build": "node -e \"const f=require('fs');f.mkdirSync('dist',{recursive:true});f.copyFileSync('src.txt','dist/app.txt')\""
                },
            }
        ),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {"": {"name": "fixture", "version": "1.0.0"}},
            }
        ),
        encoding="utf-8",
    )
    tree = _commit(repo, "node")
    project = ProjectConfig(
        "demo",
        repo,
        "/srv",
        build=BuildConfig(
            runner="host",
            commands=(("npm", "ci", "--offline", "--ignore-scripts"), ("npm", "run", "build")),
        ),
        artifacts=(ArtifactConfig("dist", "public", "tree"),),
    )
    service = BuildService(project, tmp_path / "target")
    first = service.execute(tree)
    second = service.execute(tree)
    assert [item.destination for item in first.entry.artifacts] == ["public/app.txt"]
    assert second.cache_hit is True


def test_go_offline_binary_mode_hash_and_cache(tmp_path: Path) -> None:
    """CGO-disabled Go build emits an executable Linux artifact with stable cache."""

    repo = tmp_path / "repo"
    _init(repo)
    (repo / "go.mod").write_text("module fixture\n\ngo 1.23\n", encoding="utf-8")
    (repo / "main.go").write_text(
        'package main\nimport "fmt"\nfunc main(){fmt.Println("ok")}\n',
        encoding="utf-8",
    )
    tree = _commit(repo, "go")
    project = ProjectConfig(
        "demo",
        repo,
        "/srv",
        build=BuildConfig(
            runner="host",
            commands=(("go", "build", "-o", "dist/server", "."),),
            env_allowlist=("CGO_ENABLED", "GOOS", "GOARCH"),
        ),
        artifacts=(ArtifactConfig("dist/server", "bin/server", "file"),),
    )
    environment = os.environ.copy()
    environment.update({"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64"})
    # BuildService uses the process environment through HostBuildRunner.
    old = {name: os.environ.get(name) for name in ("CGO_ENABLED", "GOOS", "GOARCH")}
    os.environ.update({key: environment[key] for key in ("CGO_ENABLED", "GOOS", "GOARCH")})
    try:
        service = BuildService(project, tmp_path / "target")
        first = service.execute(tree)
        second = service.execute(tree)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    artifact = first.entry.artifacts[0]
    assert artifact.destination == "bin/server"
    assert artifact.executable is True
    assert artifact.size > 0
    assert second.cache_hit is True
