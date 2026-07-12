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
    TransactionStore(root).create(target_id=identity.target_id, stage="prepared")
    code = run(["state", "recover", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "state_recover" in out or "decision=" in out
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
    code = run(["history", "demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "state: legacy" in out
    assert "state: v1" in out
    assert "target_id:" in out


def test_stateful_deploy_creates_journal_and_advances_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinary deploy with current uses StateDeploymentExecutor and advances generation."""

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

    def factory(_server: dict[str, object]) -> InMemoryTransport:
        return transport

    set_cli_transport_factory(factory)
    try:
        code = run(["deploy", "demo", "--revisions", newer, "--yes"])
        out = capsys.readouterr().out
        assert code == 0, out + capsys.readouterr().err
        assert "succeeded" in out or "generation=" in out
        loaded = store.load_current_state()
        assert loaded is not None
        pointer, after = loaded
        assert pointer.generation == 2
        assert transport.files["/srv/demo/file.txt"].data == b"two\n"
        # Target-scoped deployment store under target root.
        assert (root / "deployments").is_dir()
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
        code = run(["state", "recover", "demo", "--execute", "--yes"])
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


def test_dry_run_target_state_unchanged(
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
    code = run(["deploy", "demo", "--revisions", newer, "--dry-run"])
    assert code == 0
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
        code1 = run(["rollback", "demo", "--latest", "--yes"])
        assert code1 == 0
        assert factory_calls["n"] == 1
        gen = store.read_current().generation  # type: ignore[union-attr]
        writes = transport.write_calls
        reads = getattr(transport, "read_calls", 0)
        # Second request: eligibility fails before factory.
        code2 = run(["rollback", "demo", "--latest", "--yes"])
        assert code2 != 0
        assert factory_calls["n"] == 1  # no second factory call
        assert store.read_current().generation == gen  # type: ignore[union-attr]
        assert transport.write_calls == writes
        assert getattr(transport, "read_calls", 0) == reads
        assert TransactionStore(root).list_open() == []
    finally:
        set_cli_transport_factory(None)


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
