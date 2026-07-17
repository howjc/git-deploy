"""Thin-workspace discovery, prepare, sequencing, and convergence tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

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
    run_workspace_doctor,
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


def test_workspace_build_is_local_without_target_ssh_tools_or_remote_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build-only ignores remote aliases, native tools, Git, and overlapping roots."""

    first = _create_repository(
        tmp_path,
        "api",
        remote_root="/srv/app",
        alias="invalid-api",
        build_steps=('printf built > build-marker',),
    )
    second = _create_repository(
        tmp_path,
        "web",
        remote_root="/srv/app/public",
        alias="invalid-web",
        build_steps=('printf built > build-marker',),
    )
    workspace_path = _write_workspace(tmp_path, (("api", first), ("web", second)))
    workspace_path.write_text(
        workspace_path.read_text(encoding="utf-8").replace('default_target = "dev"\n', ""),
        encoding="utf-8",
    )
    workspace = load_workspace(workspace_path)

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Reject any accidental remote or Git deployment preflight."""

        raise AssertionError("workspace build must remain local")

    monkeypatch.setattr(workspace_module, "resolve_target_for_plan", forbidden)
    monkeypatch.setattr(workspace_module, "_validate_native_tools", forbidden)
    monkeypatch.setattr(workspace_module, "_validate_remote_ownership", forbidden)
    monkeypatch.setattr(workspace_module.GitRepository, "validate", forbidden)

    run_workspace_build(workspace, None)

    assert (first / "build-marker").read_text(encoding="utf-8") == "built"
    assert (second / "build-marker").read_text(encoding="utf-8") == "built"


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


def test_all_workspace_locks_are_available_before_first_build(tmp_path: Path) -> None:
    """A busy later repository fails before an earlier Build and releases prior locks."""

    first = _create_repository(
        tmp_path,
        "api",
        build_steps=('printf built > build-marker',),
    )
    second = _create_repository(tmp_path, "web")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    first_store = StateStore(GitRepository(first).common_dir())
    second_store = StateStore(GitRepository(second).common_dir())

    with TargetLock(second_store.base, "dev"):
        with pytest.raises(PlanError, match="already being deployed"):
            prepare_workspace(workspace, None, full=False, skip_build=False)

    assert not (first / "build-marker").exists()
    with TargetLock(first_store.base, "dev"):
        pass


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


@pytest.mark.parametrize(
    ("first_root", "second_root"),
    [
        ("/srv/app", "/srv/app"),
        ("/srv/app", "/srv/app/public"),
        ("/srv/app/public", "/srv/app"),
    ],
)
def test_workspace_rejects_equal_or_nested_remote_roots_before_build_or_lock(
    tmp_path: Path,
    first_root: str,
    second_root: str,
) -> None:
    """One physical remote path cannot be owned by two repository States."""

    first = _create_repository(
        tmp_path,
        "api",
        remote_root=first_root,
        build_steps=('printf built > build-marker',),
    )
    second = _create_repository(tmp_path, "web", remote_root=second_root)
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))

    with pytest.raises(ConfigError, match="overlapping remote roots"):
        prepare_workspace(workspace, None, full=False, skip_build=False)

    assert not (first / "build-marker").exists()
    for repository in (first, second):
        store = StateStore(GitRepository(repository).common_dir())
        with TargetLock(store.base, "dev"):
            pass


def test_workspace_allows_sibling_roots_and_same_text_on_different_endpoints(
    tmp_path: Path,
) -> None:
    """Ownership comparison groups by resolved protocol/host/user/port."""

    first = _create_repository(tmp_path, "api", remote_root="/srv/api", host="host-a")
    second = _create_repository(tmp_path, "web", remote_root="/srv/web", host="host-a")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    _, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)
    for item in prepared:
        item.close()

    third_root = tmp_path / "other"
    third_root.mkdir()
    third = _create_repository(third_root, "admin", remote_root="/srv/api", host="host-b")
    mixed_workspace = _write_workspace(
        tmp_path,
        (("api", first), ("admin", third)),
    )
    _, prepared = prepare_workspace(
        load_workspace(mixed_workspace), None, full=False, skip_build=True
    )
    for item in prepared:
        item.close()


def test_workspace_rejects_ftp_root_collision(tmp_path: Path) -> None:
    """FTP repositories use the same physical ownership comparison."""

    first = _create_repository(tmp_path, "api", protocol="ftp", remote_root="/public")
    second = _create_repository(tmp_path, "web", protocol="ftp", remote_root="/public/assets")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))

    with pytest.raises(ConfigError, match="overlapping remote roots"):
        prepare_workspace(workspace, None, full=False, skip_build=True)


def test_aliases_resolving_to_same_endpoint_are_grouped_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different Alias text cannot hide an overlapping resolved endpoint."""

    first = _create_repository(
        tmp_path,
        "api",
        alias="prod-api",
        remote_root="/srv/app",
        build_steps=('printf built > build-marker',),
    )
    second = _create_repository(
        tmp_path,
        "web",
        alias="prod-web",
        remote_root="/srv/app/public",
    )
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    real_run = subprocess.run

    def resolve(command, **kwargs):  # noqa: ANN001, ANN202
        """Resolve both aliases to one physical endpoint."""

        if command[0] == "ssh":
            return subprocess.CompletedProcess(
                command,
                0,
                "hostname 192.0.2.10\nuser deploy\nport 2222\n",
                "",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr("git_deploy.config.subprocess.run", resolve)
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )

    with pytest.raises(ConfigError, match="overlapping remote roots"):
        prepare_workspace(workspace, None, full=False, skip_build=False)
    assert not (first / "build-marker").exists()


def test_alias_drift_after_workspace_prepare_keeps_state_and_remote_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmation-window Alias edit aborts before master, mutation, or State."""

    repository = _create_repository(tmp_path, "api", alias="project-prod")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", repository),)))
    real_run = subprocess.run
    current_host = "192.0.2.10"
    commands: list[list[str]] = []

    def run(command, **kwargs):  # noqa: ANN001, ANN202
        """Resolve a mutable Alias and forbid no command explicitly."""

        command = list(command)
        commands.append(command)
        if (command[0] == "ssh" or command[0].endswith("/ssh")) and "-G" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                f"hostname {current_host}\nuser deploy\nport 22\n",
                "",
            )
        if command[0] in {"git", "/usr/bin/git"}:
            return real_run(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("git_deploy.config.subprocess.run", run)
    monkeypatch.setattr("git_deploy.transports.openssh_sftp.subprocess.run", run)
    monkeypatch.setattr(
        "git_deploy.transports.openssh_sftp.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    _, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)
    current_host = "192.0.2.99"

    with pytest.raises(DeployError, match="stale target"):
        execute_workspace(prepared)

    assert not any("-MNf" in command for command in commands)
    assert not any(command[0].endswith("/sftp") for command in commands)
    assert StateStore(GitRepository(repository).common_dir()).load("dev") is None


def test_combined_plan_displays_endpoint_root_commit_mode_and_frozen_bytes(
    tmp_path: Path,
) -> None:
    """One confirmation shows every physical target and commit boundary."""

    repository = _create_repository(tmp_path, "api")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", repository),)))
    target, prepared = prepare_workspace(workspace, None, full=False, skip_build=True)

    rendered = render_workspace_plan(target, prepared)

    assert "deploy@example.invalid:22:/srv/api" in rendered
    assert "Mode: FULL" in rendered
    assert "Commit: <first deployment> ->" in rendered
    assert "frozen byte(s)" in rendered
    assert "PASSWORD" not in rendered
    prepared[0].close()


def test_workspace_doctor_preflights_all_before_any_remote_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later target-resolution failure blocks earlier create-root connections."""

    first = _create_repository(tmp_path, "api")
    second = _create_repository(tmp_path, "web")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", first), ("web", second))))
    calls = 0
    original = workspace_module.resolve_target_for_plan

    def resolve(target, **kwargs):  # noqa: ANN001, ANN202
        """Fail only the later repository's target preflight."""

        if target.remote_root.as_posix() == "/srv/web":
            raise ConfigError("invalid later alias")
        return original(target, **kwargs)

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        """Record any forbidden transport creation."""

        nonlocal calls
        calls += 1
        raise AssertionError("remote transport must not be created")

    monkeypatch.setattr(workspace_module, "resolve_target_for_plan", resolve)
    monkeypatch.setattr(workspace_module, "create_transport", forbidden)

    results = run_workspace_doctor(workspace, None, create_root=True)

    assert calls == 0
    assert any(
        not result.ok and "invalid later alias" in result.detail
        for _, checks in results
        for result in checks
    )


def test_insufficient_temporary_disk_fails_before_freeze_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare reports required temporary bytes and leaves no lock held."""

    repository = _create_repository(tmp_path, "api")
    workspace = load_workspace(_write_workspace(tmp_path, (("api", repository),)))
    monkeypatch.setattr(
        "git_deploy.prepared.shutil.disk_usage",
        lambda path: SimpleNamespace(free=0),
    )

    with pytest.raises(PlanError, match="insufficient temporary disk space"):
        prepare_workspace(workspace, None, full=False, skip_build=True)

    store = StateStore(GitRepository(repository).common_dir())
    with TargetLock(store.base, "dev"):
        pass


@pytest.mark.parametrize("name", ["bad name", "line\\nbreak", "x" * 65])
def test_workspace_rejects_unsafe_repository_names(tmp_path: Path, name: str) -> None:
    """Log/temp-prefix repository labels use a small printable alphabet."""

    repository = _create_repository(tmp_path, "api")
    workspace_path = _write_workspace(tmp_path, ((name, repository),))

    with pytest.raises(ConfigError, match="name must match"):
        load_workspace(workspace_path)


def _create_repository(
    workspace: Path,
    name: str,
    *,
    target: str = "dev",
    build_steps: tuple[str, ...] = (),
    missing_output: bool = False,
    protocol: str = "sftp",
    host: str = "example.invalid",
    remote_root: str | None = None,
    alias: str | None = None,
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
    selected_root = remote_root or f"/srv/{name}"
    if protocol == "ftp":
        connection = (
            'protocol = "ftp"\n'
            f'host = "{host}"\n'
            'username = "deploy"\n'
            'password_env = "DEPLOY_FTP_PASSWORD"\n'
        )
    elif alias is not None:
        connection = f'protocol = "sftp"\nssh_host_alias = "{alias}"\n'
    else:
        connection = (
            'protocol = "sftp"\n'
            f'host = "{host}"\n'
            'username = "deploy"\n'
        )
    (root / "deploy.toml").write_text(
        (
            f'default_target = "{target}"\n\n'
            '[source]\ninclude = ["**"]\n\n'
            f"[build]\nsteps = [{steps}]\n\n"
            f"[targets.{target}]\n"
            f"{connection}"
            f'remote_root = "{selected_root}"\n\n'
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
