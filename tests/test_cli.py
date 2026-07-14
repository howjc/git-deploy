"""CLI safety and multi-project revision-selection tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.cli import run


def _git(repository: Path, *args: str) -> str:
    """Run Git in a temporary CLI test repository.

    Args:
        repository: Temporary Git working tree.
        args: Git arguments.

    Returns:
        Stripped stdout.
    """

    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _two_commit_repository(path: Path, filename: str) -> tuple[str, str]:
    """Create a repository with one modified file and return its commit range.

    Args:
        path: Repository directory to create.
        filename: Tracked file name.

    Returns:
        Source and target commit IDs.
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Tests")
    (path / filename).write_text("one\n", encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", "one")
    older = _git(path, "rev-parse", "HEAD")
    (path / filename).write_text("two\n", encoding="utf-8")
    _git(path, "commit", "-am", "two")
    return older, _git(path, "rev-parse", "HEAD")


def test_deploy_all_dry_run_is_local_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expand all ranges without attempting the intentionally invalid server connection."""

    first = tmp_path / "first"
    second = tmp_path / "second"
    _two_commit_repository(first, "one.txt")
    _two_commit_repository(second, "two.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "invalid.example"

[projects.first]
repository = "{first}"
remote_root = "/srv/first"

[projects.second]
repository = "{second}"
remote_root = "/srv/second"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    code = run(
        [
            "deploy",
            "all",
            "--revisions",
            "HEAD~1..HEAD",
            "--dry-run",
        ]
    )

    assert code == 0
    assert not (tmp_path / "state").exists()


def test_doctor_is_local_by_default_and_renders_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run standard checks without opening transport and report missing current."""

    from git_deploy.remote_verify import set_cli_transport_factory

    repository = tmp_path / "repository"
    _two_commit_repository(repository, "app.txt")
    (tmp_path / "deploy.toml").write_text(
        f"""
[server]
protocol = "sftp"
host = "doctor.example.invalid"

[projects.application]
repository = "{repository}"
remote_root = "/srv/application"
local_state_dir = "{tmp_path / 'state'}"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def forbidden(_server: dict[str, object]):
        """Fail if default doctor attempts a remote connection."""

        raise AssertionError("doctor must not connect without --check-remote")

    set_cli_transport_factory(forbidden)
    try:
        code = run(["doctor", "application"])
    finally:
        set_cli_transport_factory(None)
    output = capsys.readouterr().out

    assert code == 4
    assert "LOCAL:" in output
    assert "STATE:" in output
    assert "state.current" in output
    assert "NOT READY" in output
    assert not (tmp_path / "state").exists()


def test_removed_from_and_to_options_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose the intentional CLI break instead of silently accepting old syntax."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as error:
        run(["plan", "demo", "--from", older, "--to", newer])

    assert error.value.code == 2


def test_v02_cli_help_navigation_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep the documented v0.2 command and safety flags visible in CLI help."""

    cases = (
        (["--help"], ("build", "state")),
        (["deploy", "--help"], ("--revisions", "--remote", "--dry-run", "--check-remote")),
        (["build", "--help"], ("--revisions", "--remote")),
        (["state", "migrate", "--help"], ("--stage", "--yes", "--remote")),
        (
            ["state", "bootstrap", "--help"],
            ("--revision", "--empty", "--dry-run", "--yes", "--remote"),
        ),
        (["state", "policy-migrate", "--help"], ("--execute", "--yes", "--remote")),
        (["state", "recover", "--help"], ("--execute", "--yes", "--remote")),
        (["state", "verify", "--help"], ("--check-remote", "--remote")),
    )
    for argv, expected in cases:
        with pytest.raises(SystemExit) as stopped:
            run(argv)
        assert stopped.value.code == 0
        output = capsys.readouterr().out
        for token in expected:
            assert token in output


def test_dry_run_warns_when_worktree_deletion_is_still_present_in_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose that an uncommitted deletion cannot affect a commit-range upload."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    (repository / "file.txt").unlink()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    code = run(
        [
            "deploy",
            "demo",
            "--revisions",
            f"{older}..{newer}",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "UPLOAD file.txt" in output
    assert "uncommitted working-tree change(s) are ignored" in output
    assert "WORKTREE D file.txt (commit plan: UPLOAD)" in output


def test_cli_accepts_space_separated_non_contiguous_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pass multiple selectors through argparse into the composite planner."""

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "base")
    (repository / "one.txt").write_text("one\n", encoding="utf-8")
    _git(repository, "add", "one.txt")
    _git(repository, "commit", "-m", "one")
    first = _git(repository, "rev-parse", "HEAD")
    (repository / "skipped.txt").write_text("skipped\n", encoding="utf-8")
    _git(repository, "add", "skipped.txt")
    _git(repository, "commit", "-m", "skipped")
    (repository / "three.txt").write_text("three\n", encoding="utf-8")
    _git(repository, "add", "three.txt")
    _git(repository, "commit", "-m", "three")
    third = _git(repository, "rev-parse", "HEAD")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    code = run(["plan", "demo", "--revisions", third, first])
    output = capsys.readouterr().out

    assert code == 0
    assert "UPLOAD one.txt" in output
    assert "UPLOAD three.txt" in output
    assert "skipped.txt" not in output


def test_plan_without_revisions_reaches_application_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Treat omitted revisions as an application decision, not an argparse error."""

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    (tmp_path / "deploy.toml").write_text(
        f"""
[server]
protocol = "sftp"
host = "example.invalid"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = "{tmp_path / 'state'}"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    code = run(["plan", "demo"])

    error = capsys.readouterr().err
    assert code == 4
    assert "usage:" not in error
    assert "project 'demo' on remote 'default' has no trusted current state" in error
    assert "state bootstrap demo --revision COMMIT --remote default --yes" in error
    assert "state bootstrap demo --empty --remote default --yes" in error
    assert "Explicit --revisions" in error


def test_cli_selects_a_named_remote_for_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Require and display the named remote used by a local deployment plan."""

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"

[projects.demo]
repository = "{repository}"

[projects.demo.remotes.dev]
remote_root = "/srv/dev/demo"

[projects.demo.remotes.prod]
remote_root = "/srv/prod/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    missing_code = run(["plan", "demo", "--revisions", f"{older}..{newer}"])
    missing_output = capsys.readouterr()
    selected_code = run(
        ["plan", "demo", "--revisions", f"{older}..{newer}", "--remote", "dev"]
    )
    selected_output = capsys.readouterr().out

    assert missing_code == 4
    assert "--remote is required" in missing_output.err
    assert selected_code == 0
    assert "Remote: dev" in selected_output
    assert "UPLOAD file.txt" in selected_output


def test_remote_state_isolation_across_named_environments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep alias locks shared while isolating remote state, build, and failures."""

    from git_deploy.config import load_config, resolve_project_target, select_remote
    from git_deploy.errors import ConfigurationError
    from git_deploy.expected_state import ExpectedStateStore, build_expected_state
    from git_deploy.target_identity import default_state_base, policy_fingerprint_for_project
    from git_deploy.target_lock import TargetLock
    from git_deploy.transaction import TransactionStore

    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _two_commit_repository(alpha, "alpha.txt")
    _two_commit_repository(beta, "beta.txt")
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f'''
default_remote = "dev"

[remotes.dev]
protocol = "sftp"
host = "app.example.invalid"
username = "dev"

[remotes.dev_alias]
protocol = "sftp"
host = "APP.EXAMPLE.INVALID."
username = "alias"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"
username = "prod"

[projects.alpha]
repository = "{alpha}"
local_state_dir = ".state/alpha"

[projects.alpha.remotes.dev]
remote_root = "/srv/dev/alpha"

[projects.alpha.remotes.dev_alias]
remote_root = "/srv/dev/alpha/"

[projects.alpha.remotes.prod]
remote_root = "/srv/prod/alpha"

[projects.alpha.remotes.prod.build]
commands = [["release-tool", "build"]]
env_allowlist = ["RELEASE_TOKEN"]

[projects.alpha.remotes.prod.build.onepassword.env]
RELEASE_TOKEN = "op://example/item/token"

[projects.beta]
repository = "{beta}"
local_state_dir = ".state/beta"

[projects.beta.remotes.dev]
remote_root = "/srv/dev/beta"

[projects.beta.remotes.dev_alias]
remote_root = "/srv/dev/beta/"

[projects.beta.remotes.prod]
remote_root = "/srv/prod/beta"
'''.strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(config_path)
    _dev_name, dev_server, dev_projects = select_remote(config, "dev")
    _alias_name, alias_server, alias_projects = select_remote(config, "dev_alias")
    _prod_name, prod_server, prod_projects = select_remote(config, "prod")

    dev_ids = {
        name: resolve_project_target(dev_server, project, config=config)
        for name, project in dev_projects.items()
    }
    alias_ids = {
        name: resolve_project_target(alias_server, project, config=config)
        for name, project in alias_projects.items()
    }
    prod_ids = {
        name: resolve_project_target(prod_server, project, config=config)
        for name, project in prod_projects.items()
    }
    for name in ("alpha", "beta"):
        assert dev_ids[name].target_id == alias_ids[name].target_id
        assert dev_ids[name].physical_fingerprint == alias_ids[name].physical_fingerprint
        assert dev_ids[name].target_id != prod_ids[name].target_id

    assert dev_projects["alpha"].build is None
    prod_build = prod_projects["alpha"].build
    assert prod_build is not None and prod_build.onepassword is not None
    assert tuple(name for name, _reference in prod_build.onepassword.env) == (
        "RELEASE_TOKEN",
    )

    dev_alpha_root = dev_ids["alpha"].state_root(
        default_state_base("alpha", dev_projects["alpha"].local_state_dir)
    )
    alias_alpha_root = alias_ids["alpha"].state_root(
        default_state_base("alpha", alias_projects["alpha"].local_state_dir)
    )
    prod_alpha_root = prod_ids["alpha"].state_root(
        default_state_base("alpha", prod_projects["alpha"].local_state_dir)
    )
    assert dev_alpha_root == alias_alpha_root
    with TargetLock(dev_alpha_root):
        with pytest.raises(ConfigurationError, match="target lock unavailable"):
            TargetLock(alias_alpha_root).acquire()
        with TargetLock(prod_alpha_root):
            pass

    assert run(["plan", "all", "--revisions", "HEAD", "--remote", "dev"]) == 0
    all_output = capsys.readouterr().out
    assert "Remote: dev" in all_output
    assert f"[alpha] target_id={dev_ids['alpha'].target_id}" in all_output
    assert f"[beta] target_id={dev_ids['beta'].target_id}" in all_output
    assert prod_ids["alpha"].target_id not in all_output
    assert prod_ids["beta"].target_id not in all_output
    assert "provider=1password" not in all_output

    tree = _git(alpha, "rev-parse", "HEAD^{tree}")
    current = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(),
        physical_fingerprint=dev_ids["alpha"].physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(dev_projects["alpha"]),
        files=(),
    )
    ExpectedStateStore(dev_alpha_root, dev_ids["alpha"]).cas_advance(
        expected_generation=None, state=current
    )
    TransactionStore(dev_alpha_root).create(target_id=dev_ids["alpha"].target_id)
    prod_before = sorted(
        path.relative_to(prod_alpha_root).as_posix()
        for path in prod_alpha_root.rglob("*")
        if path.is_file()
    )

    assert run(["plan", "alpha", "--revisions", "HEAD", "--remote", "dev"]) != 0
    failed = capsys.readouterr()
    assert "unfinished transaction" in failed.err
    assert run(["plan", "alpha", "--revisions", "HEAD", "--remote", "prod"]) == 0
    prod_output = capsys.readouterr().out
    assert "Remote: prod" in prod_output
    assert "provider=1password" in prod_output
    assert "op://" not in prod_output
    prod_after = sorted(
        path.relative_to(prod_alpha_root).as_posix()
        for path in prod_alpha_root.rglob("*")
        if path.is_file()
    )
    assert prod_after == prod_before == ["target.lock"]


def _seed_state_for_cli(tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write config and a generation-1 current for CLI state tests.

    Args:
        tmp_path: Temp root.
        repository: Git repo path.
        monkeypatch: Pytest monkeypatch.

    Returns:
        Config path.
    """

    from git_deploy.expected_state import ExpectedStateStore, build_expected_state
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )
    from git_deploy.models import ProjectConfig

    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"},
        project,
    )
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository

    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    empty = GitRepository(repository).empty_tree()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(),
    )
    store.cas_advance(expected_generation=None, state=state)
    return config


def test_state_inspect_is_local_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """state inspect shows target/generation without connecting remotely."""

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    _seed_state_for_cli(tmp_path, repository, monkeypatch)
    code = run(["state", "inspect", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "state_inspect" in out or "Physical target ID" in out or "target" in out.lower()
    assert "not connected" in out
    assert "Generation: 1" in out


def _seed_current_with_managed_file(
    tmp_path: Path,
    repository: Path,
    *,
    relative_path: str,
    expected_bytes: bytes,
) -> tuple[object, object]:
    """Advance current state to include one managed file entry for verify tests.

    Args:
        tmp_path: Test temp root.
        repository: Git repository path.
        relative_path: Managed path relative to remote_root.
        expected_bytes: Expected remote content.

    Returns:
        ``(project, identity)`` for further test setup.
    """

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.models import ProjectConfig
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    import subprocess

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.object_store import ContentAddressedStore

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    pointer = store.read_current()
    assert pointer is not None
    ContentAddressedStore(root).put(expected_bytes)
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    state = build_expected_state(
        generation=2,
        parent_state_id=pointer.state_id,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path=relative_path,
                owner="source",
                content_sha256=hashlib.sha256(expected_bytes).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=1, state=state)
    return project, identity


def test_state_verify_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """state verify without --check-remote rehashes local state and stays offline."""

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    _seed_state_for_cli(tmp_path, repository, monkeypatch)
    _seed_current_with_managed_file(
        tmp_path,
        repository,
        relative_path="file.txt",
        expected_bytes=b"expected-remote\n",
    )
    code = run(["state", "verify", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "state_verify_local" in out
    assert "not connected" in out
    assert "state_verify_remote" not in out


def test_state_verify_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """state verify --check-remote reads fake transport and classifies match/drift with zero writes."""

    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    _seed_state_for_cli(tmp_path, repository, monkeypatch)
    expected_bytes = b"expected-remote\n"
    _seed_current_with_managed_file(
        tmp_path,
        repository,
        relative_path="file.txt",
        expected_bytes=expected_bytes,
    )

    transport = InMemoryTransport()
    transport.files["/srv/demo/file.txt"] = FakeRemotePath(expected_bytes)

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["state", "verify", "demo", "--check-remote"])
        out = capsys.readouterr().out
        assert code == 0
        assert "state_verify_remote" in out
        assert "write_calls=0" in out
        assert "match file.txt" in out
        assert transport.write_calls == 0
        assert transport.read_calls >= 1

        # Broken remote bytes must be classified as drift (not silently pass).
        transport.files["/srv/demo/file.txt"] = FakeRemotePath(b"tampered\n")
        code_drift = run(["state", "verify", "demo", "--check-remote"])
        out_drift = capsys.readouterr().out
        assert code_drift == 3
        assert "drift file.txt" in out_drift
        assert transport.write_calls == 0
    finally:
        set_cli_transport_factory(None)


def test_state_recover_lists_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """state recover surfaces decisions without secrets."""

    from git_deploy.target_identity import default_state_base, resolve_target_identity
    from git_deploy.models import ProjectConfig
    from git_deploy.transaction import TransactionStore

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    _seed_state_for_cli(tmp_path, repository, monkeypatch)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    transaction_store = TransactionStore(root)
    journal = transaction_store.create(target_id=identity.target_id, stage="prepared")
    transaction_store.advance(journal, "manual_recovery_required")
    code = run(["state", "recover", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "state_recover" in out or "decision=" in out
    assert "manual_recovery_required" in out
    assert "transaction=" in out and "stage=" in out
    assert "conflict path=" in out and "actual=not-read" in out
    assert "do not deploy" in out
    assert "password" not in out.lower()
    assert "token" not in out.lower()


def test_state_no_gc_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """state gc is explicitly refused with exit 4; no objects deleted."""

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    _seed_state_for_cli(tmp_path, repository, monkeypatch)
    from git_deploy.models import ProjectConfig
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    # Create a sentinel object that must remain.
    sentinel = root / "objects" / "sha256" / "ab" / "sentinel"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"keep-me")
    code = run(["state", "gc"])
    err = capsys.readouterr()
    assert code == 4
    assert "not supported" in (err.err + err.out)
    assert sentinel.is_file()
    assert sentinel.read_bytes() == b"keep-me"


def test_history_and_verify_show_state_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """history prints state lineage markers for legacy and v1 manifests."""

    from git_deploy.models import DeploymentManifest, ProjectConfig
    from git_deploy.state import DeploymentStore

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    store = DeploymentStore(project)
    store.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T000000Z-leg1",
            project="demo",
            repository=str(repository),
            remote_root="/srv/demo",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
        )
    )
    store.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T000001Z-new1",
            project="demo",
            repository=str(repository),
            remote_root="/srv/demo",
            from_commit="b",
            to_commit="c",
            created_at="t2",
            status="succeeded",
            before_state_id="sha256:before",
            after_state_id="sha256:after",
            before_generation=1,
            after_generation=2,
            target_id="tgt",
            state="v1",
            transaction_id="tx1",
        )
    )
    corrupt = store.root / "deployments" / "20200101T000002Z-bad1" / "manifest.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not-json", encoding="utf-8")
    code = run(["history", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "state: legacy" in out
    assert "state: v1" in out
    assert "target_id:" in out
    assert "1 corrupt deployment record(s)" in out
    assert str(corrupt) in out


def test_stateful_implicit_deploy_creates_journal_and_advances_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinary deploy with current uses StateDeploymentExecutor and advances generation."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state import DeploymentStore
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    from git_deploy.git_store import PersistentGitStore

    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    old_bytes = b"one\n"
    ContentAddressedStore(root).put(old_bytes)
    import hashlib
    import subprocess

    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    older_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{older}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=older_tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="file.txt",
                owner="source",
                content_sha256=hashlib.sha256(old_bytes).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)

    transport = InMemoryTransport()
    transport.files["/srv/demo/file.txt"] = FakeRemotePath(old_bytes)
    factory_calls = {"count": 0}

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        """Count connections so repeated no-op deploys prove zero remote access."""

        factory_calls["count"] += 1
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["deploy", "demo", "--yes"])
        out = capsys.readouterr().out
        assert code == 0, out + capsys.readouterr().err
        assert "succeeded" in out or "generation=" in out
        assert "risk=standard" in out
        assert "upload=1 delete=0 bytes=" in out
        assert "auto_rollback=True" in out
        loaded = store.load_current_state()
        assert loaded is not None
        pointer, after = loaded
        assert pointer.generation == 2
        assert transport.files["/srv/demo/file.txt"].data == b"two\n"
        # Target-scoped deployment store under target root.
        assert (root / "deployments").is_dir()
        manifest = DeploymentStore(project, root=root).latest_successful()
        assert manifest.revision_specs == [newer]

        assert run(["history", "demo"]) == 0
        history_output = capsys.readouterr().out
        assert newer in history_output
        assert " HEAD " not in history_output

        generation = store.read_current().generation  # type: ignore[union-attr]
        manifests = len(DeploymentStore(project, root=root).list_manifests())
        writes = transport.write_calls
        assert run(["deploy", "demo", "--yes"]) == 0
        implicit_output = capsys.readouterr().out
        assert run(["deploy", "demo", "--revisions", newer, "--yes"]) == 0
        explicit_output = capsys.readouterr().out
        assert "No changes: target generation already matches" in implicit_output
        assert "No changes: target generation already matches" in explicit_output
        assert factory_calls["count"] == 1
        assert transport.write_calls == writes
        assert store.read_current().generation == generation  # type: ignore[union-attr]
        assert len(DeploymentStore(project, root=root).list_manifests()) == manifests
    finally:
        set_cli_transport_factory(None)


def test_application_deploy_static_noop_rejects_stale_generation_via_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default ``git-deploy deploy`` static no-op must not skip lock/freshness.

    Ships the real CLI entry: application plan → DeployService → domain path.
    After the reviewed plan is produced at generation N, current is advanced to
    N+1 (non-overlapping state-only lineage change). The same deploy command must
    refuse with stale_plan / non-zero exit, zero remote writes, and current kept
    at N+1.
    """

    import hashlib
    import subprocess

    from git_deploy.cli import run
    from git_deploy.config import load_config, select_remote
    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
    )
    from git_deploy.config import resolve_project_target
    from git_deploy.application.plan_service import RevisionPlanService

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "file.txt").write_text("same\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "deploy.example"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(config_path)
    _remote, server, projects = select_remote(config, None)
    project = projects["demo"]
    identity = resolve_project_target(server, project, config=config)
    root = identity.state_root(default_state_base(project.name, project.local_state_dir))
    PersistentGitStore(root, repo).ensure_layout()
    PersistentGitStore(root, repo)._publish_repository_identity()
    body = b"same\n"
    ContentAddressedStore(root).put(body)
    store = ExpectedStateStore(root, identity)
    from git_deploy.state_composer import StateComposer

    # Real applied transition for HEAD + source tree == HEAD tree ⇒ static no-op.
    head_tid = StateComposer(repo).transition_id_for_commit(head).as_str()
    gen1 = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(head_tid,),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="file.txt",
                owner="source",
                content_sha256=hashlib.sha256(body).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=gen1)

    transport = InMemoryTransport()
    transport.files["/srv/demo/file.txt"] = FakeRemotePath(body)
    writes_before = transport.write_calls
    factory_calls = {"count": 0}

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        factory_calls["count"] += 1
        return transport

    set_cli_transport_factory(factory)

    real_plan = RevisionPlanService.plan

    def plan_then_advance(self, request):  # type: ignore[no-untyped-def]
        """Produce a static no-op plan at gen N, then advance current to N+1."""

        planned = real_plan(self, request)
        assert planned.static_noop is True
        current = store.load_current_state()
        assert current is not None
        pointer, state = current
        # Generation-only advance keeps the same tree so the reviewed plan is
        # still a static no-op of the files, but the frozen generation is stale.
        advanced = build_expected_state(
            generation=pointer.generation + 1,
            parent_state_id=pointer.state_id,
            source_tree_id=state.source_tree_id,
            applied_transition_ids=tuple(state.applied_transition_ids),
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint=policy_fingerprint_for_project(project),
            files=state.files,
            artifacts=({"mode": "probe", "note": "concurrent-advance"},),
        )
        store.cas_advance(expected_generation=pointer.generation, state=advanced)
        return planned

    monkeypatch.setattr(RevisionPlanService, "plan", plan_then_advance)
    try:
        code = run(["deploy", "demo", "--revisions", head, "--yes"])
        captured = capsys.readouterr()
        err = captured.err
        out = captured.out
        assert code != 0, f"expected stale rejection, got 0\nout={out}\nerr={err}"
        assert "stale_plan" in err, f"expected stale_plan in stderr, got:\n{err}\nstdout:\n{out}"
        assert transport.write_calls == writes_before
        # Static no-op must not open a real transport for remote mutation.
        assert factory_calls["count"] == 0
        current = store.read_current()
        assert current is not None and current.generation == 2
        state = store.read_state(current.state_id)
        assert state.artifacts and state.artifacts[0].get("mode") == "probe"
    finally:
        set_cli_transport_factory(None)


def test_stateful_rollback_non_latest_refused_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With current, non-latest rollback fails before transport factory is called."""

    from git_deploy.expected_state import ExpectedStateStore, build_expected_state
    from git_deploy.models import DeploymentManifest, ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state import DeploymentStore
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository

    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    empty = GitRepository(repository).empty_tree()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(),
    )
    store.cas_advance(expected_generation=None, state=state)
    deploy = DeploymentStore(project, root=root)
    for dep_id in ("20200101T000000Z-old1", "20200101T000001Z-new1"):
        deploy.write_manifest(
            DeploymentManifest(
                deployment_id=dep_id,
                project="demo",
                repository=str(repository),
                remote_root="/srv/demo",
                from_commit="a",
                to_commit="b",
                created_at="t",
                status="succeeded",
                snapshots=[],
                state="v1",
                target_id=identity.target_id,
            )
        )

    factory_calls = {"n": 0}

    def factory(_server: dict[str, object]):
        factory_calls["n"] += 1
        raise AssertionError("transport factory must not be called for non-latest")

    set_cli_transport_factory(factory)
    try:
        code = run(["rollback", "demo", "--deployment", "20200101T000000Z-old1", "--yes"])
        assert code != 0
        assert factory_calls["n"] == 0
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_empty_refuses_when_managed_path_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI empty bootstrap verifies managed paths; presence keeps current absent."""

    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.models import ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "managed.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
include = ["managed.txt"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    transport = InMemoryTransport()
    transport.files["/srv/demo/managed.txt"] = FakeRemotePath(b"still-here")

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["state", "bootstrap", "demo", "--empty", "--yes"])
        assert code != 0
        project = ProjectConfig(
            name="demo",
            repository=repository,
            remote_root="/srv/demo",
            local_state_dir=tmp_path / ".state/demo",
        )
        identity = resolve_target_identity(
            {"protocol": "sftp", "host": "cli.example"}, project
        )
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        assert ExpectedStateStore(root, identity).read_current() is None
        assert transport.write_calls == 0
        assert transport.read_calls >= 1
    finally:
        set_cli_transport_factory(None)


def test_policy_migrate_remote_verify_zero_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy-migrate recomputes new managed paths and read-verifies them (write=0).

    Current state only lists a.txt; source tree also has b.txt. New policy with
    include ** must read both paths before CAS, not only old state.files.
    """

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    (repository / "a.txt").write_text("one\n", encoding="utf-8")
    (repository / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repository, "add", "a.txt", "b.txt")
    _git(repository, "commit", "-m", "both")
    head_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
exclude = ["*.bak"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
        exclude=("*.bak",),
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    from git_deploy.git_store import PersistentGitStore

    store = ExpectedStateStore(root, identity)
    data = b"one\n"
    data_b = b"two\n"
    ContentAddressedStore(root).put(data)
    ContentAddressedStore(root).put(data_b)
    import hashlib

    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old_policy = "old-policy-fingerprint-0001"
    # Old state only knows a.txt; b.txt is managed under new include ** policy.
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=head_tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=old_policy,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(data).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)

    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(data)
    transport.files["/srv/demo/b.txt"] = FakeRemotePath(data_b)
    factory_calls = {"n": 0}
    read_paths: list[str] = []
    real_read = transport.read_file

    def tracking_read(remote_path: str):
        read_paths.append(remote_path)
        return real_read(remote_path)

    transport.read_file = tracking_read  # type: ignore[method-assign]

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        factory_calls["n"] += 1
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["state", "policy-migrate", "demo", "--execute", "--yes"])
        assert code == 0
        assert factory_calls["n"] >= 1
        assert transport.write_calls == 0
        assert "/srv/demo/a.txt" in read_paths
        assert "/srv/demo/b.txt" in read_paths  # new managed path must be read
        loaded = store.load_current_state()
        assert loaded is not None
        pointer, after = loaded
        assert pointer.generation == 2
        assert after.policy_fingerprint == policy_fingerprint_for_project(
            ProjectConfig(
                name="demo",
                repository=repository,
                remote_root="/srv/demo",
                exclude=("*.bak",),
            )
        )
        assert any(e.path == "b.txt" and e.exists for e in after.files)
    finally:
        set_cli_transport_factory(None)


def test_state_recover_execute_restores_partial_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI crash recovery: multi-path partial mutate then recover restores before bytes.

    Injects two planned uploads, fails after the first mutation so remote is neither
    pure before nor pure after. Asserts ``state recover --execute`` drives restore
    callbacks and restores both paths from durable journal backups.
    """

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.models import PlannedFile, ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
    from git_deploy.state_planner import SourceDiffPlan
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )
    from git_deploy.transaction import TransactionStore

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "a.txt")
    (repository / "b.txt").write_text("b-before\n", encoding="utf-8")
    _git(repository, "add", "b.txt")
    _git(repository, "commit", "-m", "add-b")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository

    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    before_a = b"a-before\n"
    before_b = b"b-before\n"
    after_a = b"a-after\n"
    after_b = b"b-after\n"
    ContentAddressedStore(root).put(before_a)
    ContentAddressedStore(root).put(before_b)
    import hashlib

    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    empty = GitRepository(repository).empty_tree()
    ha_before = hashlib.sha256(before_a).hexdigest()
    hb_before = hashlib.sha256(before_b).hexdigest()
    ha_after = hashlib.sha256(after_a).hexdigest()
    hb_after = hashlib.sha256(after_b).hexdigest()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(path="a.txt", owner="source", content_sha256=ha_before, exists=True),
            FileEntry(path="b.txt", owner="source", content_sha256=hb_before, exists=True),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)

    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(before_a)
    transport.files["/srv/demo/b.txt"] = FakeRemotePath(before_b)
    contents = {"a.txt": after_a, "b.txt": after_b}
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=transport,
        content_provider=lambda p: contents[p],
    )
    plan = SourceDiffPlan(
        before_tree_id="tree",
        after_tree_id="tree2",
        files=(),
        excluded=(),
        introduced_transition_ids=("t1",),
        applied_transition_ids=("t1",),
    )
    files = [
        PlannedFile(
            action="upload",
            path="a.txt",
            remote_path="/srv/demo/a.txt",
            source_path="a.txt",
            expected_before_sha256=ha_before,
            target_sha256=ha_after,
        ),
        PlannedFile(
            action="upload",
            path="b.txt",
            remote_path="/srv/demo/b.txt",
            source_path="b.txt",
            expected_before_sha256=hb_before,
            target_sha256=hb_after,
        ),
    ]
    after_state = executor._build_after_state(plan, state, files)
    # Persist after_state so recover classification can load after hashes.
    store.write_state(after_state)
    journal = executor.prepare(
        plan=plan,
        files=files,
        before_state=state,
        after_state=after_state,
        deployment_id="20200101T000000Z-crash1",
    )
    try:
        executor.mutate_remote(journal, files, fail_after_writes=1)
    except RuntimeError:
        pass
    open_tx = TransactionStore(root).list_open()
    assert open_tx
    assert open_tx[0].stage == "remote_mutating"
    # Partial: first path mutated, second still before.
    assert transport.files["/srv/demo/a.txt"].data == after_a
    assert transport.files["/srv/demo/b.txt"].data == before_b

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(
            [
                "state",
                "recover",
                "demo",
                "--execute",
                "--yes",
                "--confirm-phrase",
                f"CONFIRM STATE {identity.target_id}",
            ]
        )
        assert code == 0
        current = store.read_current()
        assert current is not None
        assert current.generation == 1
        open_after = TransactionStore(root).list_open()
        # Proven restore path: journal closed and both remote paths are before bytes.
        assert open_after == []
        assert transport.files["/srv/demo/a.txt"].data == before_a
        assert transport.files["/srv/demo/b.txt"].data == before_b
    finally:
        set_cli_transport_factory(None)


def test_history_lists_target_scoped_stateful_deployments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """history must surface manifests written under physical target_root deployments/."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.models import DeploymentManifest, ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state import DeploymentStore
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository

    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    ContentAddressedStore(root).put(b"x")
    import hashlib

    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    empty = GitRepository(repository).empty_tree()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="file.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"x").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    # Write only to target-scoped store (as stateful deploy does).
    target_deploy = DeploymentStore(project, root=root)
    target_deploy.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T000099Z-state1",
            project="demo",
            repository=str(repository),
            remote_root="/srv/demo",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
            before_state_id=state.state_id(),
            after_state_id=state.state_id(),
            before_generation=1,
            after_generation=2,
            target_id=identity.target_id,
            state="v1",
        )
    )
    # Legacy project store must remain empty for this probe.
    assert DeploymentStore(project).list_manifests() == []

    code = run(["history", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "20200101T000099Z-state1" in out
    assert "state: v1" in out


def test_plan_missing_cas_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary plan with current referencing missing CAS fails closed (force ignored)."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.gitrepo import GitRepository
    from git_deploy.models import ProjectConfig
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    empty = GitRepository(repository).empty_tree()
    import hashlib

    store = ExpectedStateStore(root, identity)
    # Missing CAS for this digest.
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=empty,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="file.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"missing-cas-bytes").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    code = run(["plan", "demo", "--revisions", "HEAD", "--force"])
    assert code != 0


def test_missing_git_store_plan_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan fails closed when entire target git/ store is missing."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    store = ExpectedStateStore(root, identity)
    ContentAddressedStore(root).put(b"x")
    import hashlib
    import subprocess

    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="file.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"x").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    # No git/ directory at all.
    assert not (root / "git").exists()
    code = run(["plan", "demo", "--revisions", "HEAD", "--force"])
    assert code != 0


def test_restored_deploy_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook failure restores remote but CLI deploy exit is non-zero."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
post_commands = ["false-command"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
        post_commands=("fail-hook",),
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    import hashlib
    import subprocess

    older_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{older}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    old_bytes = b"one\n"
    ContentAddressedStore(root).put(old_bytes)
    store = ExpectedStateStore(root, identity)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=older_tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="file.txt",
                owner="source",
                content_sha256=hashlib.sha256(old_bytes).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)

    class HookFailTransport(InMemoryTransport):
        supports_commands = True

        def execute(self, command: str) -> tuple[int, str, str]:
            return 1, "", "hook failed"

    transport = HookFailTransport()
    transport.files["/srv/demo/file.txt"] = FakeRemotePath(old_bytes)

    def factory(_s):
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["deploy", "demo", "--revisions", newer, "--yes"])
        assert code != 0
        assert transport.files["/srv/demo/file.txt"].data == old_bytes
        current = store.read_current()
        assert current is not None
        assert current.generation == 1
    finally:
        set_cli_transport_factory(None)


def test_policy_migrate_full_state_includes_new_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy-migrate after.files must include new policy paths with source hashes."""

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "a.txt").write_text("one\n", encoding="utf-8")
    (repository / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repository, "add", "a.txt", "b.txt")
    _git(repository, "commit", "-m", "both")
    head_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
exclude = ["*.bak"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
        exclude=("*.bak",),
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    store = ExpectedStateStore(root, identity)
    data_a = b"one\n"
    data_b = b"two\n"
    ContentAddressedStore(root).put(data_a)
    ContentAddressedStore(root).put(data_b)
    import hashlib

    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=head_tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="old-policy-fingerprint-0001",
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(data_a).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(data_a)
    transport.files["/srv/demo/b.txt"] = FakeRemotePath(data_b)

    def factory(_s):
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["state", "policy-migrate", "demo", "--execute", "--yes"])
        assert code == 0
        loaded = store.load_current_state()
        assert loaded is not None
        _p, after = loaded
        paths = {e.path: e for e in after.files if e.exists}
        assert "a.txt" in paths
        assert "b.txt" in paths
        assert paths["b.txt"].content_sha256 == hashlib.sha256(data_b).hexdigest()
        assert after.generation == 2
    finally:
        set_cli_transport_factory(None)


def test_synthetic_tree_cli_plan_deploy_b_plus_d_then_e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Product CLI plan/deploy: current=B, select D → B+D; then E → B+D+E, never C.

    Builds A–B–C–D–E with distinct path edits so B+D is not any real commit tree.
    Seeds current at B tree + B applied; first deploy applies D; second applies E.
    Asserts remote has b/d/e and not c, and synthetic tree is durable via target store.
    """

    import hashlib
    import subprocess

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_composer import StateComposer
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    commits: dict[str, str] = {}
    for name, fname in [
        ("A", "a.txt"),
        ("B", "b.txt"),
        ("C", "c.txt"),
        ("D", "d.txt"),
        ("E", "e.txt"),
    ]:
        (repository / fname).write_text(f"{name}\n", encoding="utf-8")
        _git(repository, "add", fname)
        _git(repository, "commit", "-m", name)
        commits[name] = _git(repository, "rev-parse", "HEAD")

    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    tree_b = _git(repository, "rev-parse", f"{commits['B']}^{{tree}}")
    # Seed remote + CAS for B content (a.txt + b.txt).
    cas = ContentAddressedStore(root)
    for fname, body in [("a.txt", b"A\n"), ("b.txt", b"B\n")]:
        cas.put(body)
    store = ExpectedStateStore(root, identity)
    composer = StateComposer(repository, git_store=git_store)
    tid_b = composer.transition_id_for_commit(commits["B"]).as_str()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree_b,
        applied_transition_ids=(tid_b,),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"A\n").hexdigest(),
                exists=True,
            ),
            FileEntry(
                path="b.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"B\n").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)

    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(b"A\n")
    transport.files["/srv/demo/b.txt"] = FakeRemotePath(b"B\n")

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        return transport

    set_cli_transport_factory(factory)
    try:
        # Plan D: should upload d.txt, not c.txt.
        code_plan = run(["plan", "demo", "--revisions", commits["D"]])
        plan_out = capsys.readouterr().out
        assert code_plan == 0, plan_out
        assert "UPLOAD d.txt" in plan_out
        assert "c.txt" not in plan_out.lower() or "UPLOAD c.txt" not in plan_out

        code_d = run(["deploy", "demo", "--revisions", commits["D"], "--yes"])
        out_d = capsys.readouterr().out
        assert code_d == 0, out_d
        assert transport.files.get("/srv/demo/d.txt") is not None
        assert transport.files["/srv/demo/d.txt"].data == b"D\n"
        assert "/srv/demo/c.txt" not in transport.files

        loaded = store.load_current_state()
        assert loaded is not None
        _p, after_bd = loaded
        assert after_bd.generation == 2
        # Synthetic tree must be readable via store, not required to be in main DB alone.
        git_store.require_tree(after_bd.source_tree_id)

        # Deploy E onto B+D.
        code_e = run(["deploy", "demo", "--revisions", commits["E"], "--yes"])
        out_e = capsys.readouterr().out
        assert code_e == 0, out_e
        assert transport.files.get("/srv/demo/e.txt") is not None
        assert transport.files["/srv/demo/e.txt"].data == b"E\n"
        assert "/srv/demo/c.txt" not in transport.files
        # a,b,d,e present; c absent
        for path in ("a.txt", "b.txt", "d.txt", "e.txt"):
            assert f"/srv/demo/{path}" in transport.files
        loaded2 = store.load_current_state()
        assert loaded2 is not None
        _p2, after_bde = loaded2
        assert after_bde.generation == 3
        git_store.require_tree(after_bde.source_tree_id)
        # Main-repo plain cat-file of synthetic B+D tree may fail; store env works.
        reopened = PersistentGitStore(root, repository)
        reopened.require_tree(after_bd.source_tree_id)
        reopened.require_tree(after_bde.source_tree_id)
        # Plain main object DB must not read synthetic B+D tree.
        main_rc = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-t", after_bd.source_tree_id],
            capture_output=True,
            text=True,
        ).returncode
        assert main_rc != 0, "synthetic tree must not be readable via main repo alone"
    finally:
        set_cli_transport_factory(None)


def test_policy_migrate_full_state_refuses_b_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New managed path b.txt absent/drift → migrate refused, generation unchanged."""

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "a.txt").write_text("one\n", encoding="utf-8")
    (repository / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repository, "add", "a.txt", "b.txt")
    _git(repository, "commit", "-m", "both")
    head_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
exclude = ["*.bak"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
        exclude=("*.bak",),
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    store = ExpectedStateStore(root, identity)
    data_a = b"one\n"
    ContentAddressedStore(root).put(data_a)
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=head_tree,
        applied_transition_ids=(),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint="old-policy-fingerprint-0001",
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(data_a).hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)

    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(data_a)
    # b.txt intentionally ABSENT — new managed path must refuse migrate.

    def factory(_s: dict[str, object]) -> InMemoryTransport:
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["state", "policy-migrate", "demo", "--execute", "--yes"])
        assert code != 0
        current = store.read_current()
        assert current is not None
        assert current.generation == 1
        assert transport.write_calls == 0
        # Drift case: b present but wrong content.
        transport.files["/srv/demo/b.txt"] = FakeRemotePath(b"wrong\n")
        code2 = run(["state", "policy-migrate", "demo", "--execute", "--yes"])
        assert code2 != 0
        assert store.read_current().generation == 1  # type: ignore[union-attr]
        assert transport.write_calls == 0
    finally:
        set_cli_transport_factory(None)


def _snapshot_tree(root: Path) -> dict[str, str]:
    """Return relative path -> sha256 for all files under root.

    Args:
        root: Directory to snapshot.

    Returns:
        Mapping of relative POSIX paths to content digests.
    """

    import hashlib

    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_plan_target_state_unchanged_for_synthetic_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinary plan with current=B selecting D leaves target_root file hashes unchanged."""

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_composer import StateComposer
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    commits: dict[str, str] = {}
    for name, fname in [
        ("A", "a.txt"),
        ("B", "b.txt"),
        ("C", "c.txt"),
        ("D", "d.txt"),
    ]:
        (repository / fname).write_text(f"{name}\n", encoding="utf-8")
        _git(repository, "add", fname)
        _git(repository, "commit", "-m", name)
        commits[name] = _git(repository, "rev-parse", "HEAD")

    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    tree_b = _git(repository, "rev-parse", f"{commits['B']}^{{tree}}")
    cas = ContentAddressedStore(root)
    for body in (b"A\n", b"B\n"):
        cas.put(body)
    store = ExpectedStateStore(root, identity)
    composer = StateComposer(repository, git_store=git_store)
    tid_b = composer.transition_id_for_commit(commits["B"]).as_str()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree_b,
        applied_transition_ids=(tid_b,),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"A\n").hexdigest(),
                exists=True,
            ),
            FileEntry(
                path="b.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"B\n").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=state)
    before = _snapshot_tree(root)
    code = run(["plan", "demo", "--revisions", commits["D"]])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "UPLOAD d.txt" in out
    assert "UPLOAD c.txt" not in out
    after = _snapshot_tree(root)
    assert after == before, f"plan mutated target_root: {set(after) ^ set(before)}"


def test_implicit_dry_run_matches_plan_and_leaves_target_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """deploy --dry-run with current leaves target_root unchanged."""

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    gs = PersistentGitStore(root, repository)
    gs.ensure_layout()
    gs._publish_repository_identity()
    older_tree = _git(repository, "rev-parse", f"{older}^{{tree}}")
    ContentAddressedStore(root).put(b"one\n")
    store = ExpectedStateStore(root, identity)
    store.cas_advance(
        expected_generation=None,
        state=build_expected_state(
            generation=1,
            parent_state_id=None,
            source_tree_id=older_tree,
            applied_transition_ids=(),
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint=policy_fingerprint_for_project(project),
            files=(
                FileEntry(
                    path="file.txt",
                    owner="source",
                    content_sha256=hashlib.sha256(b"one\n").hexdigest(),
                    exists=True,
                ),
            ),
        ),
    )
    before = _snapshot_tree(root)
    assert run(["plan", "demo"]) == 0
    plan_output = capsys.readouterr().out
    code = run(["deploy", "demo", "--dry-run"])
    deploy_output = capsys.readouterr().out
    assert code == 0
    assert "implicit_current_to_head" in plan_output
    assert "implicit_current_to_head" in deploy_output
    newer_tree = _git(repository, "rev-parse", f"{newer}^{{tree}}")
    assert f"target {newer_tree[:12]}" in plan_output
    assert f"target {newer_tree[:12]}" in deploy_output
    assert "UPLOAD file.txt" in plan_output
    assert "UPLOAD file.txt" in deploy_output
    assert _snapshot_tree(root) == before


def test_policy_migrate_wrong_hash_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy migrate with b wrong hash: non-zero, gen unchanged, write=0."""

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "a.txt").write_text("one\n")
    (repository / "b.txt").write_text("two\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "both")
    head = _git(repository, "rev-parse", "HEAD^{tree}")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
exclude = ["*.bak"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
        exclude=("*.bak",),
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    PersistentGitStore(root, repository).ensure_layout()
    PersistentGitStore(root, repository)._publish_repository_identity()
    store = ExpectedStateStore(root, identity)
    ContentAddressedStore(root).put(b"one\n")
    store.cas_advance(
        expected_generation=None,
        state=build_expected_state(
            generation=1,
            parent_state_id=None,
            source_tree_id=head,
            applied_transition_ids=(),
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint="old-pol",
            files=(
                FileEntry(
                    path="a.txt",
                    owner="source",
                    content_sha256=hashlib.sha256(b"one\n").hexdigest(),
                    exists=True,
                ),
            ),
        ),
    )
    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(b"one\n")
    transport.files["/srv/demo/b.txt"] = FakeRemotePath(b"WRONG\n")

    def factory(_s):
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["state", "policy-migrate", "demo", "--execute", "--yes"])
        assert code != 0
        assert store.read_current().generation == 1  # type: ignore[union-attr]
        assert transport.write_calls == 0
    finally:
        set_cli_transport_factory(None)


def test_plan_ephemeral_cleanup_after_synthetic_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary synthetic plan leaves no new /tmp/git-deploy-plan-* owned dirs."""

    import glob
    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.state_composer import StateComposer
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    commits: dict[str, str] = {}
    for name, fname in [("A", "a.txt"), ("B", "b.txt"), ("C", "c.txt"), ("D", "d.txt")]:
        (repository / fname).write_text(f"{name}\n", encoding="utf-8")
        _git(repository, "add", fname)
        _git(repository, "commit", "-m", name)
        commits[name] = _git(repository, "rev-parse", "HEAD")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    tree_b = _git(repository, "rev-parse", f"{commits['B']}^{{tree}}")
    cas = ContentAddressedStore(root)
    for body in (b"A\n", b"B\n"):
        cas.put(body)
    store = ExpectedStateStore(root, identity)
    composer = StateComposer(repository, git_store=git_store)
    tid_b = composer.transition_id_for_commit(commits["B"]).as_str()
    store.cas_advance(
        expected_generation=None,
        state=build_expected_state(
            generation=1,
            parent_state_id=None,
            source_tree_id=tree_b,
            applied_transition_ids=(tid_b,),
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint=policy_fingerprint_for_project(project),
            files=(
                FileEntry(
                    path="a.txt",
                    owner="source",
                    content_sha256=hashlib.sha256(b"A\n").hexdigest(),
                    exists=True,
                ),
                FileEntry(
                    path="b.txt",
                    owner="source",
                    content_sha256=hashlib.sha256(b"B\n").hexdigest(),
                    exists=True,
                ),
            ),
        ),
    )
    before_eph = set(glob.glob("/tmp/git-deploy-plan-*"))
    before_root = _snapshot_tree(root)
    code = run(["plan", "demo", "--revisions", commits["D"]])
    assert code == 0
    after_eph = set(glob.glob("/tmp/git-deploy-plan-*"))
    # No new ephemeral dirs owned by this plan call.
    assert after_eph - before_eph == set()
    assert _snapshot_tree(root) == before_root


def test_dry_run_ephemeral_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deploy --dry-run cleans owned ephemeral plan object dirs."""

    import glob
    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )

    repository = tmp_path / "repo"
    older, newer = _two_commit_repository(repository, "file.txt")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    gs = PersistentGitStore(root, repository)
    gs.ensure_layout()
    gs._publish_repository_identity()
    older_tree = _git(repository, "rev-parse", f"{older}^{{tree}}")
    ContentAddressedStore(root).put(b"one\n")
    store = ExpectedStateStore(root, identity)
    store.cas_advance(
        expected_generation=None,
        state=build_expected_state(
            generation=1,
            parent_state_id=None,
            source_tree_id=older_tree,
            applied_transition_ids=(),
            physical_fingerprint=identity.physical_fingerprint,
            policy_fingerprint=policy_fingerprint_for_project(project),
            files=(
                FileEntry(
                    path="file.txt",
                    owner="source",
                    content_sha256=hashlib.sha256(b"one\n").hexdigest(),
                    exists=True,
                ),
            ),
        ),
    )
    before_eph = set(glob.glob("/tmp/git-deploy-plan-*"))
    code = run(["deploy", "demo", "--revisions", newer, "--dry-run"])
    assert code == 0
    assert set(glob.glob("/tmp/git-deploy-plan-*")) - before_eph == set()


def test_plan_cleanup_exception_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner exception after ephemeral create still removes owned temp dir."""

    import glob
    import tempfile
    from pathlib import Path as P

    from git_deploy.state_composer import ComposeResult
    from git_deploy.state_planner import StatePlanner

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "a.txt").write_text("a\n")
    _git(repository, "add", "a.txt")
    _git(repository, "commit", "-m", "a")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    planner = StatePlanner(repository)
    eph = tempfile.mkdtemp(prefix="git-deploy-plan-")
    before = set(glob.glob("/tmp/git-deploy-plan-*"))
    # Simulate owned compose result with ephemeral that plan_from_compose must close.
    # Distinct trees so plan_from_compose must call repo.changes (not static_noop).
    compose = ComposeResult(
        base_tree_id=tree,
        target_tree_id="0" * 40,
        applied_transition_ids=(),
        introduced_transition_ids=("t1",),
        skipped_transition_ids=(),
        commits=(),
        object_env={"GIT_OBJECT_DIRECTORY": str(P(eph) / "objects")},
        _ephemeral_dir=eph,
    )
    (P(eph) / "objects").mkdir(parents=True, exist_ok=True)

    def boom(*_a, **_k):
        raise RuntimeError("injected plan failure")

    monkeypatch.setattr(planner.repo, "changes", boom)
    try:
        planner.plan_from_compose(compose, remote_unverified=True)
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert not P(eph).exists()
    assert set(glob.glob("/tmp/git-deploy-plan-*")) <= before


def test_plan_cleanup_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt during plan_from_compose still closes owned ephemeral."""

    import tempfile
    from pathlib import Path as P

    from git_deploy.state_composer import ComposeResult
    from git_deploy.state_planner import StatePlanner

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "a.txt").write_text("a\n")
    _git(repository, "add", "a.txt")
    _git(repository, "commit", "-m", "a")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    planner = StatePlanner(repository)
    eph = tempfile.mkdtemp(prefix="git-deploy-plan-")
    (P(eph) / "objects").mkdir(parents=True, exist_ok=True)
    compose = ComposeResult(
        base_tree_id=tree,
        target_tree_id="0" * 40,
        applied_transition_ids=("t0",),
        introduced_transition_ids=("t1",),
        skipped_transition_ids=(),
        commits=(),
        object_env={"GIT_OBJECT_DIRECTORY": str(P(eph) / "objects")},
        _ephemeral_dir=eph,
    )

    def interrupt(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(planner.repo, "changes", interrupt)
    try:
        planner.plan_from_compose(compose, remote_unverified=True)
        raised = False
    except KeyboardInterrupt:
        raised = True
    assert raised
    assert not P(eph).exists()


def test_rollback_repeat_refused_before_transport_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second CLI rollback of same deployment: factory=0, gen unchanged, no journal.

    Shipped entry: run(['rollback', ..., '--yes']) after one successful rollback.
    """

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import DeploymentManifest, FileSnapshot, ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state import DeploymentStore
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )
    from git_deploy.transaction import TransactionStore

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "a.txt").write_text("x\n")
    _git(repository, "add", "a.txt")
    _git(repository, "commit", "-m", "c")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    PersistentGitStore(root, repository).ensure_layout()
    PersistentGitStore(root, repository)._publish_repository_identity()
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    from git_deploy.object_store import ContentAddressedStore

    cas = ContentAddressedStore(root)
    cas.put(b"before")
    cas.put(b"after")
    pol = policy_fingerprint_for_project(project)
    store = ExpectedStateStore(root, identity)
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"before").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id=tree,
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"after").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=1, state=after)
    deploy = DeploymentStore(project, root=root)
    bak = deploy.write_backup("20200101T120000Z-dep", 0, b"before")
    deploy.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T120000Z-dep",
            project="demo",
            repository=str(repository),
            remote_root="/srv/demo",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
            snapshots=[
                FileSnapshot(
                    path="a.txt",
                    remote_path="/srv/demo/a.txt",
                    before_exists=True,
                    before_sha256=hashlib.sha256(b"before").hexdigest(),
                    backup_file=bak,
                    after_exists=True,
                    after_sha256=hashlib.sha256(b"after").hexdigest(),
                )
            ],
            state="v1",
            before_state_id=before.state_id(),
            after_state_id=after.state_id(),
            before_generation=1,
            after_generation=2,
            introduced_transition_ids=["t1"],
            target_id=identity.target_id,
        )
    )
    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(b"after")
    factory_calls = {"n": 0}

    def factory(_server: dict[str, object]):
        factory_calls["n"] += 1
        return transport

    set_cli_transport_factory(factory)
    try:
        code1 = run(["rollback", "demo", "--yes"])
        assert code1 == 0
        assert factory_calls["n"] == 1
        gen = store.read_current().generation  # type: ignore[union-attr]
        writes = transport.write_calls
        reads = getattr(transport, "read_calls", 0)
        # Second request: eligibility fails before factory.
        code2 = run(["rollback", "demo", "--yes"])
        assert code2 != 0
        assert factory_calls["n"] == 1  # no second factory call
        assert store.read_current().generation == gen  # type: ignore[union-attr]
        assert transport.write_calls == writes
        assert getattr(transport, "read_calls", 0) == reads
        assert TransactionStore(root).list_open() == []
    finally:
        set_cli_transport_factory(None)


def test_rollback_omitted_selector_normalizes_to_explicit_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route omitted and explicit latest selectors through the same request path."""

    from argparse import Namespace

    import git_deploy.cli as cli

    observed: list[tuple[str | None, bool]] = []

    def capture(_config: object, args: Namespace) -> int:
        """Capture the normalized selector without executing a rollback."""

        observed.append((args.deployment, args.latest))
        return 0

    monkeypatch.setattr(cli, "_run_application_latest_rollback", capture)
    common = {
        "deployment": None,
        "dry_run": False,
        "_application_gated": False,
    }
    assert cli._run_rollback(object(), Namespace(latest=False, **common)) == 0
    assert cli._run_rollback(object(), Namespace(latest=True, **common)) == 0
    assert observed == [(None, True), (None, True)]


def test_rollback_current_mismatch_refused_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI rollback when current advanced past after_state: factory=0."""

    import hashlib

    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import DeploymentManifest, FileSnapshot, ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state import DeploymentStore
    from git_deploy.target_identity import (
        default_state_base,
        policy_fingerprint_for_project,
        resolve_target_identity,
    )
    from git_deploy.transaction import TransactionStore

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repository,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    PersistentGitStore(root, repository).ensure_layout()
    PersistentGitStore(root, repository)._publish_repository_identity()
    pol = policy_fingerprint_for_project(project)
    store = ExpectedStateStore(root, identity)
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="t1",
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"b").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=None, state=before)
    after = build_expected_state(
        generation=2,
        parent_state_id=before.state_id(),
        source_tree_id="t2",
        applied_transition_ids=("t0", "t1"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=(
            FileEntry(
                path="a.txt",
                owner="source",
                content_sha256=hashlib.sha256(b"a").hexdigest(),
                exists=True,
            ),
        ),
    )
    store.cas_advance(expected_generation=1, state=after)
    # Advance current past deployment after-state (state-only style).
    advanced = build_expected_state(
        generation=3,
        parent_state_id=after.state_id(),
        source_tree_id="t3",
        applied_transition_ids=("t0", "t1", "t2"),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=pol,
        files=after.files,
    )
    store.cas_advance(expected_generation=2, state=advanced)
    deploy = DeploymentStore(project, root=root)
    bak = deploy.write_backup("20200101T130000Z-dep", 0, b"b")
    deploy.write_manifest(
        DeploymentManifest(
            deployment_id="20200101T130000Z-dep",
            project="demo",
            repository=str(repository),
            remote_root="/srv/demo",
            from_commit="a",
            to_commit="b",
            created_at="t",
            status="succeeded",
            snapshots=[
                FileSnapshot(
                    path="a.txt",
                    remote_path="/srv/demo/a.txt",
                    before_exists=True,
                    before_sha256=hashlib.sha256(b"b").hexdigest(),
                    backup_file=bak,
                    after_exists=True,
                    after_sha256=hashlib.sha256(b"a").hexdigest(),
                )
            ],
            state="v1",
            before_state_id=before.state_id(),
            after_state_id=after.state_id(),
            before_generation=1,
            after_generation=2,
            introduced_transition_ids=["t1"],
            target_id=identity.target_id,
        )
    )
    gen = store.read_current().generation  # type: ignore[union-attr]
    factory_calls = {"n": 0}

    def factory(_server: dict[str, object]):
        factory_calls["n"] += 1
        raise AssertionError("factory must not open on current mismatch")

    set_cli_transport_factory(factory)
    try:
        code = run(["rollback", "demo", "--latest", "--yes"])
        assert code != 0
        assert factory_calls["n"] == 0
        assert store.read_current().generation == gen  # type: ignore[union-attr]
        assert TransactionStore(root).list_open() == []
    finally:
        set_cli_transport_factory(None)


def test_host_build_command_is_local_only_and_cacheable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit host build materializes an exact tree, caches artifacts, and never connects."""

    import sys

    from git_deploy.cli import run
    from git_deploy.config import load_config, resolve_project_target, select_remote
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.target_identity import default_state_base

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "build.py").write_text(
        "from pathlib import Path\n"
        "Path('dist').mkdir(exist_ok=True)\n"
        "Path('dist/app.txt').write_text('artifact')\n",
        encoding="utf-8",
    )
    _git(repository, "add", "build.py")
    _git(repository, "commit", "-qm", "build")
    head = _git(repository, "rev-parse", "HEAD")
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
[projects.demo.build]
commands = [["{sys.executable}", "build.py"]]
[[projects.demo.artifacts]]
source = "dist"
destination = "public"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    calls = {"count": 0}

    def forbid_remote(_server: dict[str, object]):
        calls["count"] += 1
        raise AssertionError("build command must not open remote transport")

    set_cli_transport_factory(forbid_remote)
    try:
        assert run(["build", "demo", "--revisions", head]) == 0
        first = capsys.readouterr().out
        assert "build completed" in first
        assert "not connected" in first
        assert run(["build", "demo", "--revisions", head]) == 0
        second = capsys.readouterr().out
        assert "cache hit" in second
    finally:
        set_cli_transport_factory(None)
    assert calls["count"] == 0
    assert not (repository / "dist").exists()

    config = load_config(config_path)
    _name, server, projects = select_remote(config, None)
    project = projects["demo"]
    identity = resolve_project_target(server, project, config=config)
    target_root = identity.state_root(
        default_state_base(project.name, project.local_state_dir)
    )
    assert list((target_root / "build-cache/entries").glob("*.json"))
    assert list((target_root / "build/worktrees").iterdir()) == []


def test_host_build_dry_run_does_not_create_worktree_or_run_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinary deploy --dry-run renders build policy but has zero build/state effects."""

    import sys

    from git_deploy.cli import run

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "app.txt").write_text("app")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-qm", "app")
    head = _git(repository, "rev-parse", "HEAD")
    marker = tmp_path / "build-ran"
    (tmp_path / "deploy.toml").write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
[projects.demo.build]
commands = [["{sys.executable}", "-c", "from pathlib import Path; Path(r'{marker}').write_text('ran')"]]
[[projects.demo.artifacts]]
source = "dist"
destination = "public"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert run(["deploy", "demo", "--revisions", head, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "build runner=host" in output
    assert "warning:" in output
    assert not marker.exists()
    assert not (tmp_path / ".state").exists()


def test_docker_build_command_and_dry_run_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit Docker build resolves/runs; deploy dry-run performs zero Docker calls."""

    from git_deploy.build_runner import BuildExecutionError, BuildResult
    from git_deploy.cli import run
    from git_deploy.docker_runner import DockerImageIdentity

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "app.txt").write_text("app")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "app")
    head = _git(repository, "rev-parse", "HEAD")
    (tmp_path / "deploy.toml").write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
[projects.demo.build]
runner = "docker"
commands = [["tool", "build"]]
[projects.demo.build.docker]
image = "tool@sha256:configured"
[[projects.demo.artifacts]]
source = "dist"
destination = "public"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    calls: list[str] = []

    class FakeRunner:
        """Fake Docker runner that materializes one artifact."""

        daemon_warning = "daemon warning"

        def __init__(self, *args, **kwargs):
            del args, kwargs

        def resolve_image(self, _config):
            calls.append("inspect")
            return DockerImageIdentity("tool", "sha256:immutable")

        def run(self, worktree, _config, *, image=None):
            assert image is not None
            calls.append("run")
            (worktree / "dist").mkdir()
            (worktree / "dist/app.txt").write_text("artifact")
            return BuildResult("docker", ())

    monkeypatch.setattr("git_deploy.docker_runner.DockerBuildRunner", FakeRunner)
    monkeypatch.chdir(tmp_path)
    assert run(["build", "demo", "--revisions", head]) == 0
    assert calls == ["inspect", "run"]
    capsys.readouterr()
    assert run(["deploy", "demo", "--revisions", head, "--dry-run"]) == 0
    assert calls == ["inspect", "run"]

    class MissingRunner(FakeRunner):
        """Model a missing Docker daemon without host fallback."""

        def resolve_image(self, _config):
            calls.append("missing")
            raise BuildExecutionError("daemon unavailable", phase="image")

    monkeypatch.setattr("git_deploy.docker_runner.DockerBuildRunner", MissingRunner)
    assert run(["build", "demo", "--revisions", head]) != 0
    assert calls[-1] == "missing"


def test_docker_build_deploy_runs_before_remote_and_updates_artifact_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real deploy invokes Docker locally first, then mutates artifacts in one state."""

    import hashlib

    from git_deploy.build_runner import BuildResult
    from git_deploy.cli import run
    from git_deploy.config import load_config, resolve_project_target, select_remote
    from git_deploy.docker_runner import DockerImageIdentity
    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_composer import StateComposer
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, policy_fingerprint_for_project

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "app").write_text("app")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "app")
    head = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "docker.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv"
local_state_dir = ".state/demo"
[projects.demo.build]
runner = "docker"
commands = [["tool"]]
[projects.demo.build.docker]
image = "tool@sha256:configured"
[[projects.demo.artifacts]]
source = "dist"
destination = "public"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(config_path)
    _name, server, projects = select_remote(config, None)
    project = projects["demo"]
    identity = resolve_project_target(server, project, config=config)
    root = identity.state_root(default_state_base(project.name, project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old = b"old-artifact"
    ContentAddressedStore(root).put(old)
    current = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(StateComposer(repository).transition_id_for_commit(head).as_str(),),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry("public/app.txt", "artifact:public", hashlib.sha256(old).hexdigest()),),
        artifacts=({"build_fingerprint": "old"},),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=current)
    events: list[str] = []

    class FakeRunner:
        daemon_warning = "daemon warning"

        def __init__(self, *args, **kwargs):
            del args, kwargs

        def resolve_image(self, _config):
            events.append("inspect")
            return DockerImageIdentity("tool", "sha256:immutable")

        def run(self, worktree, _config, *, image=None):
            assert image is not None
            events.append("run")
            (worktree / "dist").mkdir()
            (worktree / "dist/app.txt").write_bytes(b"new-artifact")
            return BuildResult("docker", ())

    monkeypatch.setattr("git_deploy.docker_runner.DockerBuildRunner", FakeRunner)
    remote = InMemoryTransport()
    remote.files["/srv/public/app.txt"] = FakeRemotePath(old)

    def factory(_server):
        events.append("connect")
        return remote

    set_cli_transport_factory(factory)
    try:
        assert run(["deploy", "demo", "--revisions", head, "--yes"]) == 0
    finally:
        set_cli_transport_factory(None)
    assert events == ["inspect", "run", "connect"]
    assert remote.files["/srv/public/app.txt"].data == b"new-artifact"
    loaded = store.load_current_state()
    assert loaded is not None and loaded[1].generation == 2
    assert loaded[1].files[0].owner == "artifact:public"


def test_onepassword_build_command_and_dry_run_secret_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real build invokes fake op with cache bypass; dry-run invokes it zero times."""

    import os
    import sys

    from git_deploy.cli import run

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "build.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "assert os.environ['TOKEN']\n"
        "assert not any(k.startswith('OP_') for k in os.environ)\n"
        "Path('dist').mkdir(exist_ok=True)\n"
        "Path('dist/app.txt').write_text(os.environ['TOKEN'])\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "build")
    head = _git(repository, "rev-parse", "HEAD")
    calls = tmp_path / "op-calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    op = fake_bin / "op"
    op.write_text(
        f"#!{sys.executable}\n"
        "import os,subprocess,sys\n"
        f"open({str(calls)!r},'a').write('run\\n')\n"
        "args=sys.argv[1:]; assert args[:2]==['run','--']\n"
        "env=os.environ.copy()\n"
        "for k,v in list(env.items()):\n"
        "    if v.startswith('op://'): env[k]='CLI-SENTINEL'\n"
        "r=subprocess.run(args[2:],env=env,capture_output=True)\n"
        "sys.stdout.buffer.write(r.stdout.replace(b'CLI-SENTINEL',b'***'))\n"
        "sys.stderr.buffer.write(r.stderr.replace(b'CLI-SENTINEL',b'***'))\n"
        "raise SystemExit(r.returncode)\n",
        encoding="utf-8",
    )
    op.chmod(0o755)
    (tmp_path / "deploy.toml").write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
[projects.demo.build]
commands = [["{sys.executable}", "build.py"]]
env_allowlist = ["TOKEN"]
[projects.demo.build.onepassword.env]
TOKEN = "op://vault/item/token"
[[projects.demo.artifacts]]
source = "dist"
destination = "public"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "AUTH-SENTINEL")
    monkeypatch.chdir(tmp_path)
    assert run(["build", "demo", "--revisions", head]) == 0
    output = capsys.readouterr().out
    assert "provider=1password" in output
    assert "cache bypass" in output
    assert "op://" not in output
    assert "CLI-SENTINEL" not in output
    assert calls.read_text().splitlines() == ["run"]

    assert run(["deploy", "demo", "--revisions", head, "--dry-run"]) == 0
    dry_output = capsys.readouterr().out
    assert "provider=1password" in dry_output
    assert "env_names=['TOKEN']" in dry_output
    assert "op://" not in dry_output
    assert calls.read_text().splitlines() == ["run"]

    # Establish a trusted artifact current, then prove deploy builds before connect.
    import hashlib

    from git_deploy.config import load_config, resolve_project_target, select_remote
    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_composer import StateComposer
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, policy_fingerprint_for_project

    config = load_config(tmp_path / "deploy.toml")
    _name, server, projects = select_remote(config, None)
    project = projects["demo"]
    identity = resolve_project_target(server, project, config=config)
    root = identity.state_root(default_state_base(project.name, project.local_state_dir))
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old = b"old-secret-artifact"
    ContentAddressedStore(root).put(old)
    current = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=(StateComposer(repository).transition_id_for_commit(head).as_str(),),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(FileEntry("public/app.txt", "artifact:public", hashlib.sha256(old).hexdigest()),),
        artifacts=({"build_fingerprint": "old"},),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=current)
    remote = InMemoryTransport()
    remote.files["/srv/demo/public/app.txt"] = FakeRemotePath(old)
    connection_calls = {"count": 0}

    def factory(_server):
        connection_calls["count"] += 1
        with calls.open("a", encoding="utf-8") as handle:
            handle.write("connect\n")
        return remote

    set_cli_transport_factory(factory)
    try:
        # Routine deploy automation uses --yes even when a build injects secrets;
        # secret references and values remain excluded from output and state.
        assert run(["deploy", "demo", "--revisions", head, "--yes"]) == 0
    finally:
        set_cli_transport_factory(None)
    deploy_output = capsys.readouterr().out
    assert "op://" not in deploy_output
    assert "CLI-SENTINEL" not in deploy_output
    assert calls.read_text().splitlines() == ["run", "run", "connect"]
    assert connection_calls["count"] == 1
    assert remote.files["/srv/demo/public/app.txt"].data == b"CLI-SENTINEL"
    loaded = store.load_current_state()
    assert loaded is not None and loaded[1].generation == 2
    state_json = "".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in root.rglob("*.json")
    )
    for sensitive in ("op://vault/item/token", "AUTH-SENTINEL", "CLI-SENTINEL"):
        assert sensitive not in state_json

    # A later op failure must happen before another transport factory call.
    op.write_text(
        f"#!{sys.executable}\n"
        f"open({str(calls)!r},'a').write('fail\\n')\n"
        "raise SystemExit(19)\n",
        encoding="utf-8",
    )
    op.chmod(0o755)
    set_cli_transport_factory(factory)
    try:
        assert (
            run(
                [
                    "deploy",
                    "demo",
                    "--revisions",
                    head,
                    "--yes",
                ]
            )
            != 0
        )
    finally:
        set_cli_transport_factory(None)
    assert connection_calls["count"] == 1
    assert calls.read_text().splitlines()[-1] == "fail"


def test_host_build_deploy_bootstraps_artifacts_and_rolls_back_unified_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Product CLI builds before connect, baselines known artifacts, deploys, and rolls back."""

    import hashlib
    import sys

    from git_deploy.cli import run
    from git_deploy.config import load_config, resolve_project_target, select_remote
    from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.object_store import ContentAddressedStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.state_rollback import StateRollbackService
    from git_deploy.state_composer import StateComposer
    from git_deploy.target_identity import default_state_base, policy_fingerprint_for_project

    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "t@e")
    _git(repository, "config", "user.name", "T")
    (repository / "build.py").write_text(
        "from pathlib import Path\n"
        "Path('dist').mkdir(exist_ok=True)\n"
        "Path('dist/app.txt').write_text('artifact-' + Path('app.txt').read_text())\n",
        encoding="utf-8",
    )
    (repository / "app.txt").write_text("old", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "old")
    old_commit = _git(repository, "rev-parse", "HEAD")
    old_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    (repository / "app.txt").write_text("new", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-qm", "new")
    new_commit = _git(repository, "rev-parse", "HEAD")
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "deploy.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv"
local_state_dir = ".state/demo"
[projects.demo.build]
commands = [["{sys.executable}", "build.py"]]
[[projects.demo.artifacts]]
source = "dist"
destination = "public"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = load_config(config_path)
    _remote_name, server, projects = select_remote(config, None)
    project = projects["demo"]
    identity = resolve_project_target(server, project, config=config)
    root = identity.state_root(default_state_base(project.name, project.local_state_dir))
    git_store = PersistentGitStore(root, repository)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old_source = b"old"
    old_artifact = b"artifact-old"
    cas = ContentAddressedStore(root)
    cas.put(old_source)
    cas.put(old_artifact)
    applied = (StateComposer(repository).transition_id_for_commit(old_commit).as_str(),)
    current = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=old_tree,
        applied_transition_ids=applied,
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(
            FileEntry("app.txt", "source", hashlib.sha256(old_source).hexdigest()),
        ),
        artifacts=(),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=current)
    remote = InMemoryTransport()
    remote.files["/srv/app.txt"] = FakeRemotePath(old_source)
    remote.files["/srv/public/app.txt"] = FakeRemotePath(old_artifact)
    factory_calls = {"count": 0}

    def factory(_server: dict[str, object]):
        factory_calls["count"] += 1
        return remote

    set_cli_transport_factory(factory)
    try:
        assert run(["deploy", "demo", "--revisions", new_commit, "--yes"]) == 0
    finally:
        set_cli_transport_factory(None)
    output = capsys.readouterr().out
    assert "build completed" in output
    assert factory_calls["count"] == 1
    assert remote.files["/srv/app.txt"].data == b"new"
    assert remote.files["/srv/public/app.txt"].data == b"artifact-new"
    loaded = store.load_current_state()
    assert loaded is not None and loaded[1].generation == 3
    assert loaded[1].artifacts[0]["mode"] == "build"
    assert {entry.owner for entry in loaded[1].files} == {"source", "artifact:public"}

    rolled = StateRollbackService(project, identity, root, transport=remote).rollback_latest()
    assert rolled.status == "succeeded"
    assert remote.files["/srv/app.txt"].data == old_source
    assert remote.files["/srv/public/app.txt"].data == old_artifact
    after_rollback = store.load_current_state()
    assert after_rollback is not None and after_rollback[1].generation == 4
    assert after_rollback[1].artifacts[0]["mode"] == "known_source"
