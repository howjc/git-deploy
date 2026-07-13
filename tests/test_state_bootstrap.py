"""Bootstrap planning and command tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_deploy.errors import PolicyError
from git_deploy.models import ProjectConfig
from git_deploy.state_bootstrap import StateBootstrapService
from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
from git_deploy.target_identity import resolve_target_identity


def _git(repo: Path, *args: str) -> str:
    """Run git.

    Args:
        repo: Repo.
        args: Args.

    Returns:
        Stdout.
    """

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path) -> str:
    """Init a single-commit repo.

    Args:
        path: Path.

    Returns:
        HEAD commit.
    """

    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-m", "a")
    return _git(path, "rev-parse", "HEAD")


def test_inferred_baseline_marks_ancestry_without_generation_on_dry_run(tmp_path: Path) -> None:
    """Inferred plan marks first-parent ancestry; dry-run does not create generation."""

    repo = tmp_path / "repo"
    head = _repo(repo)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv")
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h"},
        project,
    )
    service = StateBootstrapService(project, identity, tmp_path / "target")
    plan = service.plan_inferred(head, dry_run=True)
    assert plan.generation == 1
    assert plan.mode == "revision"
    assert plan.applied_transition_ids
    assert service.store.read_current() is None


def test_command_bootstrap_revision_and_empty_and_refuse_adopt(tmp_path: Path) -> None:
    """Bootstrap verifies remote read-only before generation 1; unknown adopt refused."""

    repo = tmp_path / "repo"
    head = _repo(repo)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = tmp_path / "target"
    service = StateBootstrapService(project, identity, root)
    plan = service.plan_inferred(head, dry_run=False)
    transport = InMemoryTransport()
    # Remote matches revision content.
    transport.files["/srv/a.txt"] = FakeRemotePath(b"a\n")
    write_counter = [0]
    state = service.execute(plan, yes=True, transport=transport, write_counter=write_counter)
    assert state.generation == 1
    assert service.store.read_current() is not None
    assert write_counter[0] == 0
    assert transport.write_calls == 0
    assert any(entry.path == "a.txt" for entry in state.files)

    with pytest.raises(PolicyError, match="already exists"):
        service.execute(plan, yes=True, transport=transport)

    with pytest.raises(PolicyError, match="unknown remote adopt"):
        service.refuse_unknown_adopt()

    # Empty mode: refuse when remote path still present.
    root2 = tmp_path / "target2"
    service2 = StateBootstrapService(project, identity, root2)
    empty = service2.plan_empty(dry_run=False, managed_paths=("a.txt",))
    dirty = InMemoryTransport()
    dirty.files["/srv/a.txt"] = FakeRemotePath(b"still-here")
    with pytest.raises(PolicyError, match="already exists"):
        service2.execute(empty, yes=True, transport=dirty)

    # Empty mode: verified absent then write generation 1.
    clean = InMemoryTransport()
    write_counter2 = [0]
    state2 = service2.execute(empty, yes=True, transport=clean, write_counter=write_counter2)
    assert state2.generation == 1
    assert state2.applied_transition_ids == ()
    assert clean.write_calls == 0
    assert write_counter2[0] == 0


def test_command_bootstrap_revision_refuses_drift(tmp_path: Path) -> None:
    """Revision bootstrap blocks when remote bytes differ before any state write."""

    repo = tmp_path / "repo"
    head = _repo(repo)
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    service = StateBootstrapService(project, identity, tmp_path / "target")
    plan = service.plan_inferred(head, dry_run=False)
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"wrong\n")
    with pytest.raises(PolicyError, match="drift|refused"):
        service.execute(plan, yes=True, transport=transport)
    assert service.store.read_current() is None
    assert transport.write_calls == 0


def test_bootstrap_git_store_before_current(tmp_path: Path) -> None:
    """CLI bootstrap establishes git store markers before generation-1 is readable.

    Structural: PersistentGitStore layout exists when current is present.
    """

    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.target_identity import default_state_base, resolve_target_identity
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    # empty tree available
    from git_deploy.gitrepo import GitRepository

    empty = GitRepository(repo).empty_tree()
    project = ProjectConfig(name="demo", repository=repo, remote_root="/srv", local_state_dir=tmp_path / "st")
    identity = resolve_target_identity({"protocol": "sftp", "host": "h"}, project)
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    # Simulate CLI order: store first, then current.
    store = PersistentGitStore(root, repo)
    store.ensure_layout()
    store._publish_repository_identity()
    store.require_tree(empty)
    assert (root / "git" / "repository_path").is_file()
    assert (root / "git" / "repository_identity").is_file()


def test_bootstrap_atomic_require_tree_failure_no_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI bootstrap: require_tree failure before CAS leaves no generation-1 current.

    Drives shipped ``state bootstrap --revision --yes`` with injected require_tree
    failure; asserts exit non-zero and ExpectedStateStore.read_current() is None.
    """

    import subprocess

    from git_deploy.cli import run
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def boom(self, tree_id: str) -> None:
        raise RuntimeError("injected require_tree failure for atomic bootstrap")

    monkeypatch.setattr(PersistentGitStore, "require_tree", boom)
    transport = InMemoryTransport()
    transport.files["/srv/demo/f.txt"] = FakeRemotePath(b"x\n")
    set_cli_transport_factory(lambda _s: transport)
    try:
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
        assert code != 0
        project = ProjectConfig(
            name="demo",
            repository=repo,
            remote_root="/srv/demo",
            local_state_dir=tmp_path / ".state/demo",
        )
        identity = resolve_target_identity(
            {"protocol": "sftp", "host": "cli.example"}, project
        )
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        assert ExpectedStateStore(root, identity).read_current() is None
        assert transport.write_calls == 0
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_fault_require_tree_blocks_without_current(tmp_path: Path, monkeypatch) -> None:
    """CLI bootstrap fails closed when require_tree fails before current write."""

    from git_deploy.cli import run
    from git_deploy.git_store import PersistentGitStore
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    # Create a dummy commit so empty bootstrap uses empty tree which is fine.
    # Force require_tree failure by patching PersistentGitStore.require_tree.
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
include = []
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def boom(self, tree_id: str) -> None:
        raise RuntimeError("injected require_tree failure")

    monkeypatch.setattr(PersistentGitStore, "require_tree", boom)
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import InMemoryTransport

    set_cli_transport_factory(lambda _s: InMemoryTransport())
    try:
        # empty with include [] may refuse empty managed set; use revision path
        (repo / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # remote must match revision for bootstrap — inject matching transport
        from git_deploy.state_executor import FakeRemotePath

        tr = InMemoryTransport()
        tr.files["/srv/demo/f.txt"] = FakeRemotePath(b"x\n")
        set_cli_transport_factory(lambda _s: tr)
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
        assert code != 0
        # no current under state
        from git_deploy.target_identity import default_state_base, resolve_target_identity
        from git_deploy.models import ProjectConfig
        from git_deploy.expected_state import ExpectedStateStore

        project = ProjectConfig(name="demo", repository=repo, remote_root="/srv/demo", local_state_dir=tmp_path / ".state/demo")
        identity = resolve_target_identity({"protocol": "sftp", "host": "cli.example"}, project)
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        assert ExpectedStateStore(root, identity).read_current() is None
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_precommit_validator_failure_no_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS-pre final validator fails after remote verify → no generation-1 current.

    Covers the Round 4 post-current window by moving validation before CAS:
    first require_tree succeeds; precommit_validator under lock fails; CLI non-0.
    """

    import subprocess

    from git_deploy.cli import run
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}
    real_require = PersistentGitStore.require_tree

    def count_then_fail_on_second(self, tree_id: str) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("injected final precommit require_tree failure")
        return real_require(self, tree_id)

    monkeypatch.setattr(PersistentGitStore, "require_tree", count_then_fail_on_second)
    transport = InMemoryTransport()
    transport.files["/srv/demo/f.txt"] = FakeRemotePath(b"x\n")
    set_cli_transport_factory(lambda _s: transport)
    try:
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
        assert code != 0
        project = ProjectConfig(
            name="demo",
            repository=repo,
            remote_root="/srv/demo",
            local_state_dir=tmp_path / ".state/demo",
        )
        identity = resolve_target_identity(
            {"protocol": "sftp", "host": "cli.example"}, project
        )
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        assert ExpectedStateStore(root, identity).read_current() is None
        assert transport.write_calls == 0
        assert calls["n"] >= 2  # preparation + precommit under lock
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_post_current_no_fail_step_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful bootstrap publishes gen1 without a post-current fail window.

    require_tree is only called before CAS (prep + lock precommit); after
    success current is readable and no further require_tree is invoked.
    """

    import subprocess

    from git_deploy.cli import run
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    calls = {"n": 0}
    real_require = PersistentGitStore.require_tree

    def counting_require(self, tree_id: str) -> None:
        calls["n"] += 1
        return real_require(self, tree_id)

    monkeypatch.setattr(PersistentGitStore, "require_tree", counting_require)
    transport = InMemoryTransport()
    transport.files["/srv/demo/f.txt"] = FakeRemotePath(b"x\n")
    set_cli_transport_factory(lambda _s: transport)
    try:
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
        assert code == 0
        n_after_success = calls["n"]
        project = ProjectConfig(
            name="demo",
            repository=repo,
            remote_root="/srv/demo",
            local_state_dir=tmp_path / ".state/demo",
        )
        identity = resolve_target_identity(
            {"protocol": "sftp", "host": "cli.example"}, project
        )
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        current = ExpectedStateStore(root, identity).read_current()
        assert current is not None
        assert current.generation == 1
        # No post-current require_tree: count must not grow after success path ends.
        assert calls["n"] == n_after_success
        assert calls["n"] == 2  # prep + lock precommit only
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_fault_window_layout_no_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layout failure before CAS leaves no current (fault window 1)."""

    import subprocess

    from git_deploy.cli import run
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.git_store import PersistentGitStore
    from git_deploy.models import ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def boom_layout(self) -> None:
        raise OSError("injected ensure_layout failure")

    monkeypatch.setattr(PersistentGitStore, "ensure_layout", boom_layout)
    transport = InMemoryTransport()
    transport.files["/srv/demo/f.txt"] = FakeRemotePath(b"x\n")
    set_cli_transport_factory(lambda _s: transport)
    try:
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
        assert code != 0
        project = ProjectConfig(
            name="demo",
            repository=repo,
            remote_root="/srv/demo",
            local_state_dir=tmp_path / ".state/demo",
        )
        identity = resolve_target_identity(
            {"protocol": "sftp", "host": "cli.example"}, project
        )
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        assert ExpectedStateStore(root, identity).read_current() is None
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_fault_window_cas_publish_no_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS/current durable publish failure: CLI non-0, reopen current=None, write=0.

    R5-02 fault window 4: layout+precommit succeed; cas_advance raises.
    """

    import subprocess

    from git_deploy.cli import run
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.models import ProjectConfig
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.state_executor import FakeRemotePath, InMemoryTransport
    from git_deploy.target_identity import default_state_base, resolve_target_identity

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "c"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def boom_cas(self, **kwargs):
        raise OSError("injected cas_advance / durable publish failure")

    monkeypatch.setattr(ExpectedStateStore, "cas_advance", boom_cas)
    transport = InMemoryTransport()
    transport.files["/srv/demo/f.txt"] = FakeRemotePath(b"x\n")
    set_cli_transport_factory(lambda _s: transport)
    try:
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
        assert code != 0
        project = ProjectConfig(
            name="demo",
            repository=repo,
            remote_root="/srv/demo",
            local_state_dir=tmp_path / ".state/demo",
        )
        identity = resolve_target_identity(
            {"protocol": "sftp", "host": "cli.example"}, project
        )
        root = identity.state_root(default_state_base("demo", project.local_state_dir))
        # Reopen with fresh store handle (new process simulation).
        reopened = ExpectedStateStore(root, identity)
        assert reopened.read_current() is None
        assert transport.write_calls == 0
    finally:
        set_cli_transport_factory(None)


def test_bootstrap_after_replace_fault_leaves_recoverable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visible current after publisher failure remains guarded and recoverable.

    The fault fires after ``os.replace(current.json)``. CLI must return non-zero,
    while a prepared bootstrap journal proves whether generation 1 became visible;
    recovery then closes the evidence without remote writes or another generation.
    """

    from git_deploy.cli import run
    from git_deploy.durable_io import set_fault_hook
    from git_deploy.expected_state import ExpectedStateStore
    from git_deploy.remote_verify import set_cli_transport_factory
    from git_deploy.target_identity import default_state_base
    from git_deploy.transaction import TransactionStore

    repo = tmp_path / "repo"
    head = _repo(repo)
    config = tmp_path / "deploy.toml"
    config.write_text(
        f"""
[server]
protocol = "sftp"
host = "cli.example"
username = "u"
[projects.demo]
repository = "{repo}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    project = ProjectConfig(
        name="demo",
        repository=repo,
        remote_root="/srv/demo",
        local_state_dir=tmp_path / ".state/demo",
    )
    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "cli.example"}, project
    )
    root = identity.state_root(default_state_base("demo", project.local_state_dir))
    transport = InMemoryTransport()
    transport.files["/srv/demo/a.txt"] = FakeRemotePath(b"a\n")

    def fail_after_current_replace(stage: str, path: Path) -> None:
        """Inject the ambiguous durable-publish window for current.json."""

        if stage == "after_replace" and path.name == "current.json":
            raise RuntimeError("injected current after-replace failure")

    set_cli_transport_factory(lambda _s: transport)
    set_fault_hook(fail_after_current_replace)
    try:
        code = run(["state", "bootstrap", "demo", "--revision", head, "--yes"])
    finally:
        set_fault_hook(None)
        set_cli_transport_factory(None)

    assert code != 0
    current = ExpectedStateStore(root, identity).read_current()
    assert current is not None and current.generation == 1
    open_tx = TransactionStore(root).list_open()
    assert len(open_tx) == 1
    assert open_tx[0].stage == "prepared"
    assert open_tx[0].meta.get("kind") == "bootstrap"
    writes_before = transport.write_calls

    factory_calls = {"count": 0}

    def forbid_remote(_server: dict[str, object]):
        factory_calls["count"] += 1
        raise AssertionError("bootstrap prepared recovery must stay local")

    set_cli_transport_factory(forbid_remote)
    try:
        recover_code = run(["state", "recover", "demo", "--execute", "--yes"])
    finally:
        set_cli_transport_factory(None)
    assert recover_code == 0
    assert factory_calls["count"] == 0
    assert TransactionStore(root).list_open() == []
    assert ExpectedStateStore(root, identity).read_current().generation == 1  # type: ignore[union-attr]
    assert transport.write_calls == writes_before == 0
