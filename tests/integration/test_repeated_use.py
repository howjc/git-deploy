"""Repeated daily-style deployment lifecycle against one persistent fake remote."""

from __future__ import annotations

from pathlib import Path

from git_deploy.config import load_config
from git_deploy.deployer import execute_plan
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.planner import create_plan
from tests.conftest import commit_all, write_config
from tests.test_deployer import FakeTransport


def _deploy_once(
    root: Path,
    transport: FakeTransport,
    *,
    full: bool = False,
) -> int:
    """Plan and execute one deployment against a persistent synthetic remote.

    Args:
        root: Git project used for the repeated-use scenario.
        transport: Remote filesystem retained across deployment invocations.
        full: Whether to rebuild the remote-owned local state from current content.

    Returns:
        Number of planned file operations.
    """

    config = load_config(root / "deploy.toml")
    repository = GitRepository(root)
    store = StateStore(repository.git_dir())
    state = None if full else store.load("dev")
    plan = create_plan(config, config.target(None), repository, state, full=full)
    execute_plan(
        plan,
        config,
        repository,
        store,
        transport_factory=lambda target: transport,
    )
    return len(plan.operations)


def test_repeated_daily_workflow_converges_and_preserves_unknown_remote(
    git_project: Path,
) -> None:
    """Repeated full/incremental deployments converge without touching unknown files."""

    write_config(git_project)
    dist = git_project / "dist"
    dist.mkdir()
    asset = dist / "app.js"
    asset.write_text("asset-v1", encoding="utf-8")
    transport = FakeTransport()
    transport.files["uploads/user-avatar.png"] = b"unknown-remote-content"

    assert _deploy_once(git_project, transport) == 2
    assert _deploy_once(git_project, transport) == 0

    (git_project / "app.py").write_text("print('v2')\n", encoding="utf-8")
    asset.write_text("asset-v2", encoding="utf-8")
    commit_all(git_project, "daily update")
    assert _deploy_once(git_project, transport) == 2
    assert transport.files["app.py"] == b"print('v2')\n"
    assert transport.files["public/dist/app.js"] == b"asset-v2"

    (git_project / "app.py").rename(git_project / "main.py")
    asset.unlink()
    (dist / "chunk.js").write_text("chunk", encoding="utf-8")
    commit_all(git_project, "rename source and rotate assets")
    assert _deploy_once(git_project, transport) == 4
    assert "app.py" not in transport.files
    assert transport.files["main.py"] == b"print('v2')\n"
    assert "public/dist/app.js" not in transport.files
    assert transport.files["public/dist/chunk.js"] == b"chunk"

    state_path = git_project / ".git/git-deploy/dev.json"
    state_path.unlink()
    transport.files["manual.txt"] = b"not-owned-by-git-deploy"
    assert _deploy_once(git_project, transport, full=True) == 2
    assert transport.files["manual.txt"] == b"not-owned-by-git-deploy"
    assert transport.files["uploads/user-avatar.png"] == b"unknown-remote-content"
    assert state_path.is_file()
