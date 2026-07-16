"""Real pnpm/Composer builds composed with the v1-lite planner and fake remote."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from git_deploy.builder import run_build
from git_deploy.config import load_config
from git_deploy.deployer import execute_plan
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.planner import create_plan
from tests.conftest import commit_all, write_config
from tests.test_deployer import FakeTransport


def _initialize(root: Path) -> None:
    """Initialize one integration Git project and exclude local deploy/runtime files."""

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "integration@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Integration"], cwd=root, check=True)
    (root / ".git/info/exclude").write_text(
        "deploy.toml\ndist/\nvendor/\nruntime/\nuploads/\n.env\n",
        encoding="utf-8",
    )


def _deploy(root: Path, body: str) -> FakeTransport:
    """Build and fully deploy one real project through an in-memory remote."""

    config = load_config(write_config(root, body))
    repository = GitRepository(root)
    run_build(config.build, root)
    plan = create_plan(config, config.target(None), repository, None, full=False)
    transport = FakeTransport()
    execute_plan(
        plan,
        config,
        repository,
        StateStore(repository.git_dir()),
        transport_factory=lambda target: transport,
    )
    return transport


@pytest.mark.skipif(shutil.which("pnpm") is None, reason="pnpm is not installed")
def test_real_node_pnpm_build_and_dist_sync(tmp_path: Path) -> None:
    """A real frozen pnpm install/build produces and syncs the mapped dist tree."""

    root = tmp_path / "node"
    root.mkdir()
    _initialize(root)
    (root / "package.json").write_text(
        '{"name":"fixture","version":"1.0.0","scripts":{"build":"node build.js"}}\n',
        encoding="utf-8",
    )
    (root / "build.js").write_text(
        "require('fs').mkdirSync('dist',{recursive:true});"
        "require('fs').writeFileSync('dist/app.js','node-built');\n",
        encoding="utf-8",
    )
    subprocess.run(["pnpm", "install", "--lockfile-only"], cwd=root, check=True, capture_output=True)
    commit_all(root, "node fixture")

    transport = _deploy(
        root,
        """
[build]
steps = ["pnpm install --frozen-lockfile", "pnpm run build"]

[[outputs]]
local = "dist"
remote = "public/dist"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    assert transport.files["public/dist/app.js"] == b"node-built"


@pytest.mark.skipif(shutil.which("composer") is None, reason="Composer is not installed")
def test_real_php_composer_build_sync_and_protection(tmp_path: Path) -> None:
    """A real Composer install syncs PHP/vendor while protected runtime files stay untouched."""

    root = tmp_path / "php"
    root.mkdir()
    _initialize(root)
    (root / "composer.json").write_text('{"name":"fixture/php","require":{}}\n', encoding="utf-8")
    (root / "index.php").write_text("<?php echo 'ok';\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=no\n", encoding="utf-8")
    (root / "runtime").mkdir()
    (root / "runtime/session").write_text("private", encoding="utf-8")
    subprocess.run(
        ["composer", "install", "--no-interaction", "--no-progress"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    shutil.rmtree(root / "vendor")
    commit_all(root, "php fixture")

    transport = _deploy(
        root,
        """
[build]
steps = ["composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction"]

[[outputs]]
local = "vendor"
remote = "vendor"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    assert transport.files["index.php"] == b"<?php echo 'ok';\n"
    assert "vendor/autoload.php" in transport.files
    assert ".env" not in transport.files
    assert "runtime/session" not in transport.files


@pytest.mark.skipif(
    shutil.which("pnpm") is None or shutil.which("composer") is None,
    reason="pnpm and Composer are both required",
)
def test_real_mixed_project_builds_in_order_and_syncs_both_outputs(tmp_path: Path) -> None:
    """A PHP+Node project runs both toolchains and merges dist/vendor without conflicts."""

    root = tmp_path / "mixed"
    root.mkdir()
    _initialize(root)
    (root / "package.json").write_text(
        '{"name":"mixed","version":"1.0.0","scripts":{"build":"node build.js"}}\n',
        encoding="utf-8",
    )
    (root / "build.js").write_text(
        "require('fs').mkdirSync('dist',{recursive:true});"
        "require('fs').writeFileSync('dist/app.js','mixed-built');\n",
        encoding="utf-8",
    )
    (root / "composer.json").write_text('{"name":"fixture/mixed","require":{}}\n', encoding="utf-8")
    (root / "index.php").write_text("<?php\n", encoding="utf-8")
    subprocess.run(["pnpm", "install", "--lockfile-only"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["composer", "install", "--no-interaction", "--no-progress"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    shutil.rmtree(root / "vendor")
    commit_all(root, "mixed fixture")

    transport = _deploy(
        root,
        """
[build]
steps = [
  "pnpm install --frozen-lockfile",
  "pnpm run build",
  "composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction"
]

[[outputs]]
local = "dist"
remote = "public/dist"

[[outputs]]
local = "vendor"
remote = "vendor"

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    assert transport.files["public/dist/app.js"] == b"mixed-built"
    assert "vendor/autoload.php" in transport.files
    assert transport.files["index.php"] == b"<?php\n"
