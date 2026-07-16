"""Thin-workspace discovery, prepare, sequencing, and convergence tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import git_deploy.cli as cli
import git_deploy.workspace as workspace_module
from git_deploy.doctor import DoctorResult
from git_deploy.errors import ConfigError, DeployError, PlanError
from git_deploy.git import GitRepository
from git_deploy.lock import TargetLock
from git_deploy.manifest import StateStore
from git_deploy.transports.base import ProgressCallback, Transport
from git_deploy.workspace import (
    execute_workspace,
    load_workspace,
    prepare_workspace,
    render_workspace_plan,
    run_workspace_build,
)


class WorkspaceTransport(Transport):
    """Persist fake remote files and optionally fail one repository."""

    def __init__(self, files: dict[str, bytes], *, fail: bool = False) -> None:
        """Bind a repository's remote file map and failure switch."""

        self.files = files
        self.fail = fail
        self.connects = 0

    def connect(self) -> None:
        """Record a fake connection."""

        self.connects += 1

    def ensure_root(self) -> None:
        """Accept the synthetic remote root."""

    def root_exists(self) -> bool:
        """Report that the synthetic remote root exists."""

        return True

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Upload frozen bytes or simulate a terminal repository failure."""

        if self.fail:
            raise OSError("workspace repository interrupted")
        content = local_path.read_bytes()
        self.files[remote_path] = content
        callback(len(content), len(content))

    def delete(self, remote_path: str) -> None:
        """Delete one fake remote file idempotently."""

        self.files.pop(remote_path, None)

    def close(self) -> None:
        """Release no resources in the in-memory adapter."""


def test_workspace_config_preserves_order_and_rejects_extra_fields(tmp_path: Path) -> None:
    """Workspace owns only target/name/path/order and keeps declared order."""

    first = _create_repository(tmp_path, "api")
    second = _create_repository(tmp_path, "web")
    path = _write_workspace(tmp_path, (("web", second), ("api", first)))

    workspace = load_workspace(path)

    assert [item.name for item in workspace.repositories] == ["web", "api"]
    assert [item.path for item in workspace.repositories] == [second, first]
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'default_target = "dev"',
            'default_target = "dev"\nshared_target = "forbidden"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown workspace field"):
        load_workspace(path)


def test_automatic_discovery_rejects_ambiguity_and_explicit_workspace_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory containing both file types requires an explicit selector."""

    repository = _create_repository(tmp_path, "api")
    workspace_path = _write_workspace(tmp_path, (("api", repository),))
    (tmp_path / "deploy.toml").write_text("invalid = true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--dry-run", "--skip-build"]) == 2
    assert "both deploy.toml" in capsys.readouterr().err
    assert (
        cli.main(
            ["--workspace", str(workspace_path), "--dry-run", "--skip-build"]
        )
        == 0
    )


def test_workspace_cli_confirms_once_after_combined_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One interactive answer approves all already-prepared repositories."""

    first = _create_repository(tmp_path, "api")
    second = _create_repository(tmp_path, "web")
    workspace_path = _write_workspace(tmp_path, (("api", first), ("web", second)))
    prompts: list[str] = []

    def confirm(prompt: str) -> str:
        """Record the sole combined prompt and approve it."""

        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(
        cli,
        "execute_workspace",
        lambda prepared, **kwargs: tuple(item.name for item in prepared),
    )

    assert cli.main(["--workspace", str(workspace_path), "--skip-build"]) == 0
    assert len(prompts) == 1
    assert "across 2 repositories" in prompts[0]


def test_workspace_cli_routes_doctor_and_aggregates_named_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Workspace doctor uses the shared target and renders repository groups."""

    repository = _create_repository(tmp_path, "api")
    workspace_path = _write_workspace(tmp_path, (("api", repository),))
    observed: list[tuple[str | None, bool]] = []

    def diagnose(workspace, target, *, create_root):  # noqa: ANN001, ANN202
        """Return a deterministic successful workspace diagnostic."""

        observed.append((target, create_root))
        return ((workspace.repositories[0].name, (DoctorResult("target", True, "ready"),)),)

    monkeypatch.setattr(cli, "run_workspace_doctor", diagnose)

    assert (
        cli.main(
            ["--workspace", str(workspace_path), "doctor", "dev", "--create-root"]
        )
        == 0
    )
    assert observed == [("dev", True)]
    assert "[api]" in capsys.readouterr().out


def test_all_targets_are_validated_before_first_workspace_build(tmp_path: Path) -> None:
    """A missing shared target in a later repository prevents every build."""

    first = _create_repository(
        tmp_path,
        "api",
        target="prod",
        build_steps=('printf built > build-marker',),
    )
    second = _create_repository(tmp_path, "web", target="dev")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))

    with pytest.raises(ConfigError, match="unknown target 'prod'"):
        run_workspace_build(workspace, "prod")

    assert not (first / "build-marker").exists()


def test_prepare_failure_releases_earlier_locks_before_any_execution(tmp_path: Path) -> None:
    """A later missing output aborts Prepare All and releases prior target locks."""

    first = _create_repository(tmp_path, "api")
    second = _create_repository(tmp_path, "web", missing_output=True)
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))

    with pytest.raises(PlanError, match="configured output does not exist"):
        prepare_workspace(workspace, None, full=False, skip_build=True)

    store = StateStore(GitRepository(first).common_dir())
    with TargetLock(store.base, "dev"):
        pass
    assert store.load("dev") is None


def test_sequential_failure_then_workspace_rerun_converges(tmp_path: Path) -> None:
    """A succeeds, B fails, C waits; rerun makes A no-op and completes B/C."""

    repositories = tuple(_create_repository(tmp_path, name) for name in ("api", "web", "admin"))
    workspace = load_workspace(
        _write_workspace(tmp_path, tuple(zip(("api", "web", "admin"), repositories, strict=True)))
    )
    remotes: dict[str, dict[str, bytes]] = {
        "/srv/api": {},
        "/srv/web": {},
        "/srv/admin": {},
    }

    def interrupted_factory(target):  # noqa: ANN001, ANN202
        """Fail only the second repository's transport."""

        root = target.remote_root.as_posix()
        return WorkspaceTransport(remotes[root], fail=root == "/srv/web")

    _, first_run = prepare_workspace(workspace, None, full=False, skip_build=True)
    with pytest.raises(DeployError, match="workspace repository interrupted"):
        execute_workspace(first_run, transport_factory=interrupted_factory)

    states = tuple(StateStore(GitRepository(path).common_dir()) for path in repositories)
    assert states[0].load("dev") is not None
    assert states[1].load("dev") is None
    assert states[2].load("dev") is None

    _, rerun = prepare_workspace(workspace, None, full=False, skip_build=True)
    assert len(rerun[0].plan.operations) == 0
    assert len(rerun[1].plan.operations) > 0
    assert len(rerun[2].plan.operations) > 0

    def successful_factory(target):  # noqa: ANN001, ANN202
        """Return successful persistent transports for the rerun."""

        return WorkspaceTransport(remotes[target.remote_root.as_posix()])

    assert execute_workspace(rerun, transport_factory=successful_factory) == (
        "api",
        "web",
        "admin",
    )
    assert all(store.load("dev") is not None for store in states)
    assert remotes["/srv/api"]["app.py"] == b"print('api')\n"


def test_combined_plan_and_frozen_bytes_survive_later_worktree_change(tmp_path: Path) -> None:
    """Combined preview order and uploaded bytes remain bound to prepared HEADs."""

    first = _create_repository(tmp_path, "api")
    second = _create_repository(tmp_path, "web")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    target, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)
    rendered = render_workspace_plan(target, prepared)
    (first / "app.py").write_text("print('changed after confirmation')\n", encoding="utf-8")
    remotes = {"/srv/api": {}, "/srv/web": {}}

    assert rendered.index("[api]") < rendered.index("[web]") < rendered.index("Total:")
    execute_workspace(
        prepared,
        transport_factory=lambda selected: WorkspaceTransport(
            remotes[selected.remote_root.as_posix()]
        ),
    )
    assert remotes["/srv/api"]["app.py"] == b"print('api')\n"


def test_workspace_execution_passes_one_command_scoped_pool_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequential execution gives every project the same pool and closes it once."""

    first = _create_repository(tmp_path, "api")
    second = _create_repository(tmp_path, "web")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    _, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)
    pools: list[object] = []
    order: list[str] = []

    class FakePool:
        """Record command-scope cleanup without opening OpenSSH."""

        closed = 0

        def close_all(self) -> None:
            """Record exactly one final pool cleanup."""

            self.closed += 1

    pool = FakePool()

    def fake_execute(item, **kwargs):  # noqa: ANN001, ANN202
        """Observe sequential project execution and pool identity."""

        order.append(item.name)
        pools.append(kwargs["connection_pool"])

    monkeypatch.setattr(workspace_module, "SSHConnectionPool", lambda: pool)
    monkeypatch.setattr(workspace_module, "execute_prepared", fake_execute)

    assert execute_workspace(prepared) == ("api", "web")
    assert order == ["api", "web"]
    assert pools == [pool, pool]
    assert pool.closed == 1


def _create_repository(
    workspace: Path,
    name: str,
    *,
    target: str = "dev",
    build_steps: tuple[str, ...] = (),
    missing_output: bool = False,
) -> Path:
    """Create one independent Git project and minimal deploy configuration."""

    root = workspace / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Tests")
    (root / ".git/info/exclude").write_text("deploy.toml\ndist/\n", encoding="utf-8")
    (root / "app.py").write_text(f"print('{name}')\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "initial")
    steps = ", ".join(f'"{step}"' for step in build_steps)
    output = ""
    if missing_output:
        output = '\n[[outputs]]\nlocal = "dist"\nremote = "public/dist"\n'
    (root / "deploy.toml").write_text(
        (
            f'default_target = "{target}"\n\n'
            '[source]\ninclude = ["**"]\n\n'
            f"[build]\nsteps = [{steps}]\n\n"
            f"[targets.{target}]\n"
            'protocol = "sftp"\n'
            'host = "example.invalid"\n'
            'username = "deploy"\n'
            f'remote_root = "/srv/{name}"\n\n'
            '[deploy]\nretries = 1\nretry_delay = 0\n'
            f"{output}"
        ),
        encoding="utf-8",
    )
    return root


def _write_workspace(
    root: Path,
    repositories: tuple[tuple[str, Path], ...],
) -> Path:
    """Write an ordered thin-workspace file using relative repository paths."""

    lines = ['default_target = "dev"']
    for name, path in repositories:
        lines.extend(
            (
                "",
                "[[repositories]]",
                f'name = "{name}"',
                f'path = "{path.relative_to(root).as_posix()}"',
            )
        )
    workspace = root / "deploy.workspace.toml"
    workspace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workspace


def _git(root: Path, *arguments: str) -> None:
    """Run one quiet Git setup command."""

    subprocess.run(["git", *arguments], cwd=root, check=True)
