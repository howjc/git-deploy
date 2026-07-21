"""FTP Hybrid bootstrap enumeration, preflight, execution, and safety tests."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import pytest

import git_deploy.bootstrap as bootstrap_module
from git_deploy.bootstrap import (
    BootstrapAction,
    BootstrapItem,
    bootstrap_exit_code,
    collect_known_target_names,
    collect_workspace_known_target_names,
    confirm_bootstrap,
    enumerate_project_bootstrap_candidates,
    enumerate_workspace_bootstrap_candidates,
    execute_bootstrap,
    execute_bootstrap_item,
    mutation_count,
    plan_bootstrap_items,
    preflight_bootstrap_item,
    render_bootstrap_plan,
    render_bootstrap_summary,
    run_bootstrap,
    validate_bootstrap_target_filter,
)
from git_deploy.lock import TargetLock
from git_deploy.config import TargetConfig, load_config
from git_deploy.errors import ConfigError, DeployError
from git_deploy.ftp_hybrid import (
    FTP_CAPABILITY_SCHEMA,
    FTPHybridCapabilities,
    CapabilityProfileStatus,
    capability_profile_path,
    inspect_capability_profile,
    save_capability_profile,
)
from git_deploy.git import GitRepository
from git_deploy.manifest import StateStore
from git_deploy.transports.base import RemotePathType
from git_deploy.transports.ftp import FTPTransport
from git_deploy.workspace import load_workspace
from tests.conftest import write_config


def _ftp_hybrid_config(
    root: Path,
    *,
    targets: str | None = None,
    project_id: str = "github.com/acme/bootstrap-project",
) -> Path:
    """Write a Project deploy.toml with Hybrid output and FTP targets.

    Args:
        root: Project root.
        targets: Optional TOML body for ``[targets.*]`` tables.
        project_id: Hybrid project identity (host/path form).

    Returns:
        Path to the written ``deploy.toml``.
    """

    body = targets or """
[targets.prod]
protocol = "ftp"
host = "ftp-a.example"
username = "deploy"
remote_root = "/public_html"
password_env = "FTP_PROD"

[targets.staging]
protocol = "ftp"
host = "ftp-b.example"
username = "deploy"
remote_root = "/staging"
password_env = "FTP_STAGING"

[targets.sftp_box]
protocol = "sftp"
host = "sftp.example"
username = "deploy"
remote_root = "/srv"
strict_host_key_checking = true
"""
    hybrid_local = root / ".deploy" / "frontend-root"
    hybrid_local.mkdir(parents=True, exist_ok=True)
    (hybrid_local / "index.html").write_text("ok\n", encoding="utf-8")
    return write_config(
        root,
        f"""
project_id = "{project_id}"
default_target = "prod"

[source]
include = ["**"]

[build]
steps = []

[[outputs]]
name = "frontend-root"
mode = "hybrid"
local = ".deploy/frontend-root"
remote = "."

{body.strip()}

[deploy]
retries = 1
retry_delay = 0
""",
        create_outputs=False,
    )


def _valid_profile(target: TargetConfig, banner: str = "a" * 64) -> FTPHybridCapabilities:
    """Build an all-true Schema 3 profile for tests.

    Args:
        target: Target identity bound into the profile.
        banner: Server banner hash.

    Returns:
        Fully valid capability profile.
    """

    return FTPHybridCapabilities(
        FTP_CAPABILITY_SCHEMA,
        target.fingerprint,
        banner,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        100,
        True,
        True,
        True,
    )


class FakeBootstrapTransport(FTPTransport):
    """Record connect lifecycle and expose configurable root/banner/pending."""

    def __init__(
        self,
        target: TargetConfig,
        *,
        root_exists: bool = True,
        banner: str = "220 fixture",
        connect_error: str | None = None,
        pending: Any = None,
        fail_probe: str | None = None,
    ) -> None:
        """Configure synthetic FTP Hybrid preflight behavior.

        Args:
            target: Bound target configuration.
            root_exists: Whether the configured remote root exists.
            banner: Welcome banner used for identity hashing.
            connect_error: Optional connection failure message.
            pending: Optional object returned as Pending (truthy blocks).
            fail_probe: When set, probe_and_save path raises this message.
        """

        super().__init__(target)
        self._root_exists = root_exists
        self._banner = banner
        self._connect_error = connect_error
        self._pending = pending
        self.fail_probe = fail_probe
        self.connects = 0
        self.closed = 0
        self.ensure_root_calls = 0
        self.probe_calls = 0
        self.business_writes = 0

    def connect(self) -> None:
        """Record connection without opening a socket."""

        self.connects += 1
        if self._connect_error:
            raise DeployError(self._connect_error)
        self.ftp = self  # type: ignore[assignment]

    def close(self) -> None:
        """Record session close."""

        self.closed += 1
        self.ftp = None

    def getwelcome(self) -> str:
        """Return the configured welcome banner."""

        return self._banner

    def server_banner_hash(self) -> str:
        """Return a stable 64-char hex identity for fixture banners."""

        import hashlib

        from git_deploy.transports.ftp import normalize_ftp_server_banner

        welcome = normalize_ftp_server_banner(self._banner)
        if not welcome.strip():
            raise DeployError("FTP server banner lacks stable identity material")
        return hashlib.sha256(welcome.encode()).hexdigest()

    def root_exists(self) -> bool:
        """Report configured root presence."""

        return self._root_exists

    def ensure_root(self) -> None:
        """Mark the synthetic root as created."""

        self.ensure_root_calls += 1
        self._root_exists = True

    def features(self) -> frozenset[str]:
        """Advertise Hybrid-required features."""

        return frozenset({"MLSD", "UTF8"})

    def lstat(
        self,
        remote_path: str,
        *,
        allow_case_collisions: bool = False,
    ) -> RemotePathType:
        """Report pending/path absence for bootstrap safety checks.

        Args:
            remote_path: Relative path under the configured root.
            allow_case_collisions: Unused fixture flag.

        Returns:
            ``MISSING`` so Pending is treated as absent unless overridden.
        """

        del remote_path, allow_case_collisions
        if self._pending is not None:
            return RemotePathType.FILE
        return RemotePathType.MISSING


def _factory_map(
    mapping: dict[str, FakeBootstrapTransport],
) -> Callable[[TargetConfig], FTPTransport]:
    """Build a transport factory that returns pre-built fakes by target name.

    Args:
        mapping: Target name to fake transport.

    Returns:
        Factory compatible with bootstrap injection.
    """

    def factory(target: TargetConfig) -> FTPTransport:
        """Return the fake bound to the requested target name."""

        transport = mapping[target.name]
        # Re-bind target identity if resolve_target_for_plan rewrote fields.
        transport.target = target
        return transport

    return factory


def test_inspect_capability_profile_classifies_status(tmp_path: Path) -> None:
    """Profile status distinguishes missing, valid, old schema, and banner drift."""

    target = TargetConfig(
        "prod",
        "ftp",
        "ftp.example",
        "deploy",
        PurePosixPath("/root"),
        21,
        password_env="FTP_PASSWORD",
    )
    banner = "a" * 64
    assert (
        inspect_capability_profile(tmp_path, target, server_banner_hash=banner)
        is CapabilityProfileStatus.MISSING
    )
    save_capability_profile(tmp_path, _valid_profile(target, banner))
    assert (
        inspect_capability_profile(tmp_path, target, server_banner_hash=banner)
        is CapabilityProfileStatus.VALID
    )
    assert (
        inspect_capability_profile(tmp_path, target, server_banner_hash="b" * 64)
        is CapabilityProfileStatus.BANNER_DRIFT
    )
    path = capability_profile_path(tmp_path, target)
    path.write_text("not-json", encoding="utf-8")
    assert (
        inspect_capability_profile(tmp_path, target, server_banner_hash=banner)
        is CapabilityProfileStatus.CORRUPT
    )
    legacy = {
        "schema": 2,
        "target_fingerprint": target.fingerprint,
        "server_banner_hash": banner,
        "features": {
            "mlsd": True,
            "case_sensitive_paths": True,
            "retr": True,
            "rename_cross_directory": True,
            "rename_replace_file": True,
            "delete_file": True,
            "remove_directory": True,
        },
        "probed_at": 1,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert (
        inspect_capability_profile(tmp_path, target, server_banner_hash=banner)
        is CapabilityProfileStatus.OLD_SCHEMA
    )


def test_enumerate_project_skips_sftp_and_non_hybrid(git_project: Path) -> None:
    """Only FTP Hybrid targets become candidates; SFTP/non-hybrid are SKIP."""

    path = _ftp_hybrid_config(git_project)
    config = load_config(path)
    items = enumerate_project_bootstrap_candidates(config)
    by_name = {item.target_name: item for item in items}
    assert by_name["prod"].action is BootstrapAction.PROBE
    assert by_name["staging"].action is BootstrapAction.PROBE
    assert by_name["sftp_box"].action is BootstrapAction.SKIP
    assert "sftp" in by_name["sftp_box"].reason

    incremental = write_config(git_project)
    no_hybrid = load_config(incremental)
    items = enumerate_project_bootstrap_candidates(no_hybrid)
    assert all(item.action is BootstrapAction.SKIP for item in items)
    assert all("hybrid" in item.reason for item in items)


def test_enumerate_respects_target_filter(git_project: Path) -> None:
    """Positional target filters mark other targets as filtered SKIP."""

    config = load_config(_ftp_hybrid_config(git_project))
    items = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )
    by_name = {item.target_name: item for item in items}
    assert by_name["prod"].action is BootstrapAction.PROBE
    assert by_name["staging"].action is BootstrapAction.SKIP
    assert by_name["staging"].reason == "filtered"


def test_preflight_ready_probe_create_root_and_force(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight resolves READY, PROBE, CREATE_ROOT, and --force REPROBE."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    candidates = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )
    item = candidates[0]
    assert item.action is BootstrapAction.PROBE
    banner_hash = FakeBootstrapTransport(item.target).server_banner_hash()  # type: ignore[arg-type]
    save_capability_profile(item.state_base, _valid_profile(item.target, banner_hash))  # type: ignore[arg-type]

    ready_transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    ready = preflight_bootstrap_item(
        item,
        transport_factory=lambda t: ready_transport,
    )
    assert ready.action is BootstrapAction.READY

    force = preflight_bootstrap_item(
        item,
        force=True,
        transport_factory=lambda t: FakeBootstrapTransport(t),
    )
    assert force.action is BootstrapAction.REPROBE
    assert "forced" in force.reason

    missing_profile = preflight_bootstrap_item(
        replace(item, state_base=item.state_base / "other"),
        transport_factory=lambda t: FakeBootstrapTransport(t),
    )
    assert missing_profile.action is BootstrapAction.PROBE

    create = preflight_bootstrap_item(
        item,
        create_root=True,
        transport_factory=lambda t: FakeBootstrapTransport(t, root_exists=False),
    )
    assert create.action is BootstrapAction.CREATE_ROOT_AND_PROBE

    no_create = preflight_bootstrap_item(
        item,
        create_root=False,
        transport_factory=lambda t: FakeBootstrapTransport(t, root_exists=False),
    )
    assert no_create.action is BootstrapAction.FAIL_PRECHECK


def test_preflight_missing_password_is_fail_precheck(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing password env fails precheck without connecting."""

    monkeypatch.delenv("FTP_PROD", raising=False)
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    created = 0

    def forbidden(target: TargetConfig) -> FTPTransport:
        """Record illegal transport construction."""

        nonlocal created
        created += 1
        raise AssertionError("must not connect without credentials")

    result = preflight_bootstrap_item(item, transport_factory=forbidden)
    assert result.action is BootstrapAction.FAIL_PRECHECK
    assert "password" in result.reason
    assert created == 0


def test_project_bootstrap_two_targets_one_ready_one_probe(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project mode: one valid profile stays READY; missing profile is probed."""

    monkeypatch.setenv("FTP_PROD", "secret-prod")
    monkeypatch.setenv("FTP_STAGING", "secret-staging")
    config = load_config(_ftp_hybrid_config(git_project))
    candidates = [
        item
        for item in enumerate_project_bootstrap_candidates(config)
        if item.target_name in {"prod", "staging"}
    ]
    prod = next(item for item in candidates if item.target_name == "prod")
    staging = next(item for item in candidates if item.target_name == "staging")
    banner = FakeBootstrapTransport(prod.target).server_banner_hash()  # type: ignore[arg-type]
    save_capability_profile(prod.state_base, _valid_profile(prod.target, banner))  # type: ignore[arg-type]

    transports = {
        "prod": FakeBootstrapTransport(prod.target),  # type: ignore[arg-type]
        "staging": FakeBootstrapTransport(staging.target),  # type: ignore[arg-type]
    }
    saved: list[str] = []

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Record probe without remote mutation and write a real profile."""

        del now
        assert isinstance(transport, FakeBootstrapTransport)
        transport.probe_calls += 1
        path = save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )
        saved.append(target.name)
        return path

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    plan = plan_bootstrap_items(
        tuple(candidates),
        transport_factory=_factory_map(transports),
    )
    by_name = {item.target_name: item for item in plan}
    assert by_name["prod"].action is BootstrapAction.READY
    assert by_name["staging"].action is BootstrapAction.PROBE
    assert mutation_count(plan) == 1

    results = execute_bootstrap(plan, transport_factory=_factory_map(transports))
    assert bootstrap_exit_code(results) == 0
    assert saved == ["staging"]
    assert transports["prod"].probe_calls == 0
    assert transports["staging"].probe_calls == 1
    summary = render_bootstrap_summary(results)
    assert "READY" in summary
    assert "staging" in summary


def test_continue_on_error_preserves_success(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First target failure does not block later targets; exit stays non-zero."""

    monkeypatch.setenv("FTP_PROD", "secret")
    monkeypatch.setenv("FTP_STAGING", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    candidates = tuple(
        item
        for item in enumerate_project_bootstrap_candidates(config)
        if item.target_name in {"prod", "staging"}
    )
    transports = {
        name: FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
        for name, item in ((i.target_name, i) for i in candidates)
    }

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Fail prod; succeed staging."""

        del now
        if target.name == "prod":
            raise DeployError("MLSD unsupported")
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    plan = plan_bootstrap_items(candidates, transport_factory=_factory_map(transports))
    results = execute_bootstrap(plan, transport_factory=_factory_map(transports))
    assert bootstrap_exit_code(results) == 1
    by_name = {result.item.target_name: result for result in results}
    assert by_name["prod"].success is False
    assert by_name["staging"].success is True
    assert capability_profile_path(
        by_name["staging"].item.state_base,
        by_name["staging"].item.target,  # type: ignore[arg-type]
    ).is_file()


def test_bootstrap_safety_no_business_side_effects(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap path never invokes build/upload/ownership/state helpers."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    state_path = item.state_base / f"{item.target_name}.json"
    assert not state_path.exists()

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Save only the capability profile."""

        del now
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    # Ensure dangerous modules are not imported for side effects via run path.
    plan = plan_bootstrap_items((item,), transport_factory=lambda t: transport)
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert bootstrap_exit_code(results) == 0
    assert not state_path.exists()
    assert transport.business_writes == 0
    assert transport.ensure_root_calls == 0
    # Password must never appear in rendered plan/summary.
    text = render_bootstrap_plan(plan) + render_bootstrap_summary(results)
    assert "secret" not in text
    assert os.environ["FTP_PROD"] == "secret"


def test_idempotent_second_run_ready(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second bootstrap with a valid profile plans READY and performs zero probes."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    banner = transport.server_banner_hash()
    save_capability_profile(item.state_base, _valid_profile(item.target, banner))  # type: ignore[arg-type]
    probes = 0

    def fake_probe(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Count illegal second-run probes."""

        nonlocal probes
        probes += 1
        raise AssertionError("second run must not probe")

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    plan = plan_bootstrap_items((item,), transport_factory=lambda t: transport)
    assert plan[0].action is BootstrapAction.READY
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert probes == 0
    assert bootstrap_exit_code(results) == 0


def test_force_reprobe_runs_probe(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` re-probes even when the profile is currently valid."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    save_capability_profile(
        item.state_base,
        _valid_profile(item.target, transport.server_banner_hash()),  # type: ignore[arg-type]
    )
    probes = 0

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Count forced probes."""

        nonlocal probes
        del now
        probes += 1
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    plan = plan_bootstrap_items(
        (item,),
        force=True,
        transport_factory=lambda t: transport,
    )
    assert plan[0].action is BootstrapAction.REPROBE
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert probes == 1
    assert bootstrap_exit_code(results) == 0


def test_create_root_and_no_create_root(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing roots create once by default and fail under ``--no-create-root``."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target, root_exists=False)  # type: ignore[arg-type]

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Persist profile after root creation."""

        del now
        assert transport.root_exists()
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    plan = plan_bootstrap_items(
        (item,),
        create_root=True,
        transport_factory=lambda t: transport,
    )
    assert plan[0].action is BootstrapAction.CREATE_ROOT_AND_PROBE
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert transport.ensure_root_calls == 1
    assert bootstrap_exit_code(results) == 0

    transport2 = FakeBootstrapTransport(item.target, root_exists=False)  # type: ignore[arg-type]
    plan2 = plan_bootstrap_items(
        (item,),
        create_root=False,
        transport_factory=lambda t: transport2,
    )
    assert plan2[0].action is BootstrapAction.FAIL_PRECHECK


def test_confirm_requires_yes_on_non_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-interactive stdin without ``--yes`` refuses mutating bootstrap."""

    item = BootstrapItem(
        "repo",
        Path("."),
        Path("deploy.toml"),
        "prod",
        None,
        Path("."),
        BootstrapAction.PROBE,
        "profile missing",
    )
    monkeypatch.setattr(bootstrap_module.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ConfigError, match="--yes"):
        confirm_bootstrap((item,), yes=False)
    confirm_bootstrap((item,), yes=True)


def test_workspace_enumeration_independent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace enumerates multiple repos with independent state bases."""

    ws = tmp_path / "workspace"
    ws.mkdir()
    repos = []
    for name in ("frontend", "admin", "api"):
        root = ws / name
        root.mkdir()
        # Minimal git repo
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Tests"],
            cwd=root,
            check=True,
        )
        (root / "app.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
        if name == "api":
            write_config(root)  # SFTP incremental only
        else:
            _ftp_hybrid_config(
                root,
                targets=f"""
[targets.prod]
protocol = "ftp"
host = "ftp-{name}.example"
username = "deploy"
remote_root = "/{name}"
password_env = "FTP_{name.upper()}"
""",
                project_id=f"github.com/acme/{name}",
            )
        repos.append(f'{{ name = "{name}", path = "{name}" }}')
    (ws / "deploy.workspace.toml").write_text(
        "default_target = \"prod\"\n\n"
        + "\n".join(f"[[repositories]]\nname = \"{n}\"\npath = \"{n}\"\n" for n in ("frontend", "admin", "api")),
        encoding="utf-8",
    )
    workspace = load_workspace(ws / "deploy.workspace.toml")
    items = enumerate_workspace_bootstrap_candidates(workspace)
    assert len(items) >= 3
    ftp_items = [item for item in items if item.action is not BootstrapAction.SKIP]
    skip_items = [item for item in items if item.action is BootstrapAction.SKIP]
    assert {item.repository_name for item in ftp_items} == {"frontend", "admin"}
    assert any(item.repository_name == "api" for item in skip_items)
    bases = {item.state_base for item in ftp_items}
    assert len(bases) == 2


def test_run_bootstrap_end_to_end_with_yes(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_bootstrap`` prints plan+summary and returns 0 for a successful probe."""

    monkeypatch.setenv("FTP_PROD", "secret")
    path = _ftp_hybrid_config(
        git_project,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp-a.example"
username = "deploy"
remote_root = "/public_html"
password_env = "FTP_PROD"
""",
    )
    transport = FakeBootstrapTransport(
        TargetConfig(
            "prod",
            "ftp",
            "ftp-a.example",
            "deploy",
            PurePosixPath("/public_html"),
            21,
            password_env="FTP_PROD",
        )
    )

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Persist profile for the end-to-end path."""

        del now
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    code = run_bootstrap(
        config_path=path,
        yes=True,
        transport_factory=lambda t: transport,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "FTP HYBRID BOOTSTRAP PLAN" in out
    assert "FTP HYBRID BOOTSTRAP SUMMARY" in out
    assert "READY" in out


def test_doctor_still_uses_probe_and_save(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor probe path still saves profiles through the shared service."""

    from git_deploy.doctor import run_doctor
    from git_deploy.config import load_config as load
    from tests.test_doctor import MissingRootTransport

    # Doctor SFTP path regression: still works without bootstrap flags.
    config = load(write_config(git_project))
    repository = GitRepository(git_project)
    transport = MissingRootTransport()
    results = run_doctor(
        config,
        config.target(None),
        repository,
        StateStore(repository.common_dir()),
        create_root=True,
        transport_factory=lambda target: transport,
    )
    assert next(item for item in results if item.name == "target").ok
    assert transport.created == 1


def test_unknown_target_filter_fails_before_connect(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown filter names fail with ConfigError and zero remote connections."""

    path = _ftp_hybrid_config(git_project)
    config = load_config(path)
    validate_bootstrap_target_filter(None, collect_known_target_names(config))
    validate_bootstrap_target_filter(
        frozenset({"prod"}),
        collect_known_target_names(config),
    )
    with pytest.raises(ConfigError, match="unknown bootstrap target filter") as raised:
        validate_bootstrap_target_filter(
            frozenset({"prdo"}),
            collect_known_target_names(config),
        )
    assert "prdo" in str(raised.value)
    with pytest.raises(ConfigError, match="typo") as mixed:
        validate_bootstrap_target_filter(
            frozenset({"prod", "typo"}),
            collect_known_target_names(config),
        )
    assert "prod" not in str(mixed.value) or "typo" in str(mixed.value)
    assert "typo" in str(mixed.value)

    connects = 0

    def forbidden(target: TargetConfig) -> FTPTransport:
        """Record illegal connect attempts for unknown filters."""

        nonlocal connects
        connects += 1
        raise AssertionError("must not connect for unknown filters")

    with pytest.raises(ConfigError, match="prdo"):
        run_bootstrap(
            config_path=path,
            target_filter=("prdo",),
            yes=True,
            transport_factory=forbidden,
        )
    assert connects == 0

    with pytest.raises(ConfigError, match="typo"):
        run_bootstrap(
            config_path=path,
            target_filter=("prod", "typo"),
            yes=True,
            transport_factory=forbidden,
        )
    assert connects == 0


def test_workspace_unknown_filter_fails_before_connect(tmp_path: Path) -> None:
    """Workspace rejects filters absent from every repository before connect."""

    import subprocess

    ws = tmp_path / "workspace"
    ws.mkdir()
    root = ws / "frontend"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    _ftp_hybrid_config(
        root,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp.example"
username = "deploy"
remote_root = "/pub"
password_env = "FTP_PROD"
""",
    )
    (ws / "deploy.workspace.toml").write_text(
        'default_target = "prod"\n\n[[repositories]]\nname = "frontend"\npath = "frontend"\n',
        encoding="utf-8",
    )
    workspace = load_workspace(ws / "deploy.workspace.toml")
    known = collect_workspace_known_target_names(workspace)
    assert "prod" in known
    with pytest.raises(ConfigError, match="missing-target"):
        validate_bootstrap_target_filter(frozenset({"missing-target"}), known)


def test_project_non_git_fails_without_creating_dot_git(tmp_path: Path) -> None:
    """Non-Git projects fail precheck and never create a pseudo ``.git`` tree."""

    root = tmp_path / "not-git"
    root.mkdir()
    path = _ftp_hybrid_config(root)
    config = load_config(path)
    items = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )
    prod = next(item for item in items if item.target_name == "prod")
    assert prod.action is BootstrapAction.FAIL_PRECHECK
    assert "git" in prod.reason.lower()
    assert not (root / ".git").exists()
    assert not (root / ".git-deploy-invalid-worktree").exists()

    connects = 0

    def forbidden(target: TargetConfig) -> FTPTransport:
        """Forbid remote work for invalid worktrees."""

        nonlocal connects
        connects += 1
        raise AssertionError("non-git project must not connect")

    code = run_bootstrap(config_path=path, yes=True, transport_factory=forbidden)
    assert code != 0
    assert connects == 0
    assert not (root / ".git").exists()


def test_workspace_one_non_git_repo_fails_while_others_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace keeps bootstrapping later repos when one is not a Git worktree."""

    import subprocess

    ws = tmp_path / "workspace"
    ws.mkdir()
    bad = ws / "broken"
    bad.mkdir()
    _ftp_hybrid_config(
        bad,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp-broken.example"
username = "deploy"
remote_root = "/broken"
password_env = "FTP_BROKEN"
""",
        project_id="github.com/acme/broken",
    )
    good = ws / "frontend"
    good.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=good, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=good,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=good, check=True)
    (good / "app.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=good, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=good, check=True)
    _ftp_hybrid_config(
        good,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp-frontend.example"
username = "deploy"
remote_root = "/frontend"
password_env = "FTP_FRONTEND"
""",
        project_id="github.com/acme/frontend",
    )
    (ws / "deploy.workspace.toml").write_text(
        'default_target = "prod"\n\n'
        '[[repositories]]\nname = "broken"\npath = "broken"\n\n'
        '[[repositories]]\nname = "frontend"\npath = "frontend"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FTP_BROKEN", "secret")
    monkeypatch.setenv("FTP_FRONTEND", "secret")
    workspace = load_workspace(ws / "deploy.workspace.toml")
    items = enumerate_workspace_bootstrap_candidates(workspace)
    by_repo = {item.repository_name: item for item in items if item.target_name == "prod"}
    assert by_repo["broken"].action is BootstrapAction.FAIL_PRECHECK
    assert by_repo["frontend"].action is BootstrapAction.PROBE
    assert not (bad / ".git").exists()

    good_item = by_repo["frontend"]
    transport = FakeBootstrapTransport(good_item.target)  # type: ignore[arg-type]
    probes = 0

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Count probes for the healthy repository only."""

        nonlocal probes
        del now
        probes += 1
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )

    def factory(target: TargetConfig) -> FTPTransport:
        """Serve only the healthy frontend target."""

        if target.name != "prod" or "frontend" not in str(target.remote_root):
            raise AssertionError("broken repo must not reach factory")
        return transport

    plan = plan_bootstrap_items(items, transport_factory=factory)
    results = execute_bootstrap(plan, transport_factory=factory)
    assert bootstrap_exit_code(results) == 1
    assert probes == 1
    assert any(
        result.item.repository_name == "broken" and not result.success for result in results
    )
    assert any(
        result.item.repository_name == "frontend" and result.success for result in results
    )


def test_factory_exception_releases_lock_and_continues(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory errors become per-item FAIL, release locks, and keep the batch going."""

    monkeypatch.setenv("FTP_PROD", "secret")
    monkeypatch.setenv("FTP_STAGING", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    candidates = tuple(
        item
        for item in enumerate_project_bootstrap_candidates(config)
        if item.target_name in {"prod", "staging"}
    )
    plan = plan_bootstrap_items(
        candidates,
        transport_factory=lambda t: FakeBootstrapTransport(t),
    )
    staging_transport = FakeBootstrapTransport(
        next(item.target for item in plan if item.target_name == "staging")  # type: ignore[misc]
    )
    probes = 0

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Probe only the surviving target."""

        nonlocal probes
        del now
        probes += 1
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    calls = 0

    def factory(target: TargetConfig) -> FTPTransport:
        """Fail construction for prod; succeed for staging."""

        nonlocal calls
        calls += 1
        if target.name == "prod":
            raise RuntimeError("synthetic factory failure")
        return staging_transport

    results = execute_bootstrap(plan, transport_factory=factory)
    assert bootstrap_exit_code(results) == 1
    by_name = {result.item.target_name: result for result in results}
    assert by_name["prod"].success is False
    assert "factory failure" in (by_name["prod"].error or "")
    assert by_name["staging"].success is True
    assert probes == 1
    # Lock must be re-acquirable immediately after the failed item.
    prod_item = next(item for item in plan if item.target_name == "prod")
    lock = TargetLock(prod_item.state_base, "prod")
    lock.acquire()
    lock.release()


def test_ready_revalidates_after_plan_profile_deleted(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READY fails when the profile disappears between plan and execution."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    banner = transport.server_banner_hash()
    path = save_capability_profile(
        item.state_base,
        _valid_profile(item.target, banner),  # type: ignore[arg-type]
    )
    plan = plan_bootstrap_items((item,), transport_factory=lambda t: transport)
    assert plan[0].action is BootstrapAction.READY
    path.unlink()
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert bootstrap_exit_code(results) == 1
    assert results[0].success is False
    assert "no longer valid" in (results[0].error or "")


def test_ready_revalidates_pending_after_plan(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READY fails when Pending appears after the plan confirmation window."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    save_capability_profile(
        item.state_base,
        _valid_profile(item.target, transport.server_banner_hash()),  # type: ignore[arg-type]
    )
    plan = plan_bootstrap_items((item,), transport_factory=lambda t: transport)
    assert plan[0].action is BootstrapAction.READY

    def pending_block(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Simulate Pending appearing after confirmation."""

        return "FTP Hybrid Pending phase PREPARED; finish deploy/recover before bootstrap"

    monkeypatch.setattr(bootstrap_module, "_pending_block_reason", pending_block)
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert bootstrap_exit_code(results) == 1
    assert results[0].success is False
    assert "Pending" in (results[0].error or "")


def test_force_reprobe_does_not_bypass_pending(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` still refuses probe when Pending is present at execution."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    save_capability_profile(
        item.state_base,
        _valid_profile(item.target, transport.server_banner_hash()),  # type: ignore[arg-type]
    )
    plan = plan_bootstrap_items(
        (item,),
        force=True,
        transport_factory=lambda t: transport,
    )
    assert plan[0].action is BootstrapAction.REPROBE
    assert "pending" in plan[0].reason.lower()

    def pending_block(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Block force reprobe when Pending exists."""

        return "FTP Hybrid Pending phase FILES_PUBLISHED; finish deploy/recover before bootstrap"

    monkeypatch.setattr(bootstrap_module, "_pending_block_reason", pending_block)
    probes = 0

    def fake_probe(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Count illegal probes under Pending."""

        nonlocal probes
        probes += 1
        raise AssertionError("must not probe over Pending")

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert bootstrap_exit_code(results) == 1
    assert probes == 0
    assert "Pending" in (results[0].error or "")


def test_lock_acquire_permission_error_fails_item_and_continues(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock mkdir/open PermissionError becomes FAIL without aborting the batch."""

    monkeypatch.setenv("FTP_PROD", "secret")
    monkeypatch.setenv("FTP_STAGING", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    candidates = tuple(
        item
        for item in enumerate_project_bootstrap_candidates(config)
        if item.target_name in {"prod", "staging"}
    )
    plan = plan_bootstrap_items(
        candidates,
        transport_factory=lambda t: FakeBootstrapTransport(t),
    )
    staging_transport = FakeBootstrapTransport(
        next(item.target for item in plan if item.target_name == "staging")  # type: ignore[misc]
    )
    probes = 0

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Probe only the surviving staging target."""

        nonlocal probes
        del now
        probes += 1
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )

    original_acquire = TargetLock.acquire

    def acquire_maybe_perm(self: TargetLock) -> None:
        """Fail only the prod lock with a filesystem PermissionError."""

        if self.target == "prod":
            raise PermissionError("simulated lock parent not writable")
        return original_acquire(self)

    monkeypatch.setattr(TargetLock, "acquire", acquire_maybe_perm)

    def factory(target: TargetConfig) -> FTPTransport:
        """Serve staging only; prod must never reach the factory."""

        if target.name == "prod":
            raise AssertionError("prod must fail before transport construction")
        return staging_transport

    results = execute_bootstrap(plan, transport_factory=factory)
    assert bootstrap_exit_code(results) == 1
    by_name = {result.item.target_name: result for result in results}
    assert by_name["prod"].success is False
    assert "not writable" in (by_name["prod"].error or "")
    assert by_name["staging"].success is True
    assert probes == 1


def test_lock_acquire_oserror_on_open_fails_item(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock file open OSError is converted to a per-item FAIL result."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    plan = plan_bootstrap_items(
        (item,),
        transport_factory=lambda t: FakeBootstrapTransport(t),
    )

    def acquire_oserror(self: TargetLock) -> None:
        """Simulate open/fsync style OSError during lock acquisition."""

        raise OSError(28, "No space left on device")

    monkeypatch.setattr(TargetLock, "acquire", acquire_oserror)
    results = execute_bootstrap(
        plan,
        transport_factory=lambda t: (_ for _ in ()).throw(AssertionError("no factory")),
    )
    assert len(results) == 1
    assert results[0].success is False
    assert "No space left" in (results[0].error or "")


def test_lock_release_oserror_preserves_success(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release I/O errors after a successful probe do not rewrite the result."""

    monkeypatch.setenv("FTP_PROD", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    item = enumerate_project_bootstrap_candidates(
        config,
        target_filter=frozenset({"prod"}),
    )[0]
    transport = FakeBootstrapTransport(item.target)  # type: ignore[arg-type]
    plan = plan_bootstrap_items((item,), transport_factory=lambda t: transport)

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Save a valid profile for the successful path."""

        del now
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    original_release = TargetLock.release

    def release_oserror(self: TargetLock) -> None:
        """Raise after the real unlock so advisory locks are still cleared."""

        original_release(self)
        raise OSError("simulated release flush failure")

    monkeypatch.setattr(TargetLock, "release", release_oserror)
    results = execute_bootstrap(plan, transport_factory=lambda t: transport)
    assert bootstrap_exit_code(results) == 0
    assert results[0].success is True
    assert results[0].profile_path is not None


def test_execute_bootstrap_outer_isolation_continues_on_escape(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected escapes from one item become FAIL and later items still run."""

    monkeypatch.setenv("FTP_PROD", "secret")
    monkeypatch.setenv("FTP_STAGING", "secret")
    config = load_config(_ftp_hybrid_config(git_project))
    candidates = tuple(
        item
        for item in enumerate_project_bootstrap_candidates(config)
        if item.target_name in {"prod", "staging"}
    )
    plan = plan_bootstrap_items(
        candidates,
        transport_factory=lambda t: FakeBootstrapTransport(t),
    )
    staging_transport = FakeBootstrapTransport(
        next(item.target for item in plan if item.target_name == "staging")  # type: ignore[misc]
    )
    probes = 0

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Probe the staging survivor only."""

        nonlocal probes
        del now
        probes += 1
        return save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    original = bootstrap_module.execute_bootstrap_item

    def flaky_item(item, *, transport_factory=None):  # noqa: ANN001, ANN202
        """Raise outside the normal FAIL path for prod only."""

        if item.target_name == "prod":
            raise RuntimeError("unexpected escape from execute_bootstrap_item")
        return original(item, transport_factory=transport_factory)

    monkeypatch.setattr(bootstrap_module, "execute_bootstrap_item", flaky_item)
    results = execute_bootstrap(
        plan,
        transport_factory=lambda t: staging_transport if t.name == "staging" else FakeBootstrapTransport(t),
    )
    assert bootstrap_exit_code(results) == 1
    by_name = {result.item.target_name: result for result in results}
    assert by_name["prod"].success is False
    assert "unexpected escape" in (by_name["prod"].error or "")
    assert by_name["staging"].success is True
    assert probes == 1


def test_plan_print_broken_pipe_fails_before_mutation(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken plan output refuses confirmation and remote work."""

    monkeypatch.setenv("FTP_PROD", "secret")
    path = _ftp_hybrid_config(
        git_project,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp-a.example"
username = "deploy"
remote_root = "/public_html"
password_env = "FTP_PROD"
""",
    )
    probes = 0

    def fake_probe(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Count illegal probes after plan output failure."""

        nonlocal probes
        probes += 1
        raise AssertionError("must not probe when plan print fails")

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )
    confirmed = False

    def mark_confirm(items, *, yes):  # noqa: ANN001, ANN202
        """Detect whether confirmation ran after a broken plan print."""

        nonlocal confirmed
        del items, yes
        confirmed = True

    def broken_plan_print(text: object = "", *args: object, **kwargs: object) -> None:
        """Raise BrokenPipeError on the plan block only."""

        del args, kwargs
        if "FTP HYBRID BOOTSTRAP PLAN" in str(text):
            raise BrokenPipeError("plan sink closed")

    with pytest.raises(ConfigError, match="plan output failed"):
        run_bootstrap(
            config_path=path,
            yes=True,
            transport_factory=lambda t: FakeBootstrapTransport(t),
            confirm=mark_confirm,
            print_fn=broken_plan_print,
        )
    assert confirmed is False
    assert probes == 0


def test_summary_print_broken_pipe_preserves_exit_code(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summary stream failures after success do not change the exit code."""

    monkeypatch.setenv("FTP_PROD", "secret")
    path = _ftp_hybrid_config(
        git_project,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp-a.example"
username = "deploy"
remote_root = "/public_html"
password_env = "FTP_PROD"
""",
    )
    transport = FakeBootstrapTransport(
        TargetConfig(
            "prod",
            "ftp",
            "ftp-a.example",
            "deploy",
            PurePosixPath("/public_html"),
            21,
            password_env="FTP_PROD",
        )
    )
    profile_paths: list[Path] = []

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Persist profile and record the path for post-run assertions."""

        del now
        saved = save_capability_profile(
            runtime_base,
            _valid_profile(target, transport.server_banner_hash()),
        )
        profile_paths.append(saved)
        return saved

    monkeypatch.setattr(
        bootstrap_module,
        "probe_and_save_ftp_hybrid_capabilities",
        fake_probe,
    )

    def summary_pipe(text: object = "", *args: object, **kwargs: object) -> None:
        """Allow plan output; fail only on summary emission."""

        del args, kwargs
        rendered = str(text)
        if "FTP HYBRID BOOTSTRAP SUMMARY" in rendered:
            raise BrokenPipeError("summary sink closed")
        if rendered == "":
            raise OSError("blank line after pipe closed")

    code = run_bootstrap(
        config_path=path,
        yes=True,
        transport_factory=lambda t: transport,
        print_fn=summary_pipe,
    )
    assert code == 0
    assert profile_paths and profile_paths[0].is_file()


def test_summary_print_failure_preserves_nonzero_exit(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed items still return non-zero even when summary printing breaks."""

    monkeypatch.setenv("FTP_PROD", "secret")
    path = _ftp_hybrid_config(
        git_project,
        targets="""
[targets.prod]
protocol = "ftp"
host = "ftp-a.example"
username = "deploy"
remote_root = "/public_html"
password_env = "FTP_PROD"
""",
    )

    def factory(target: TargetConfig) -> FTPTransport:
        """Force a connect-time failure so the item is FAIL."""

        del target
        raise DeployError("synthetic connect failure")

    def summary_pipe(text: object = "", *args: object, **kwargs: object) -> None:
        """Break only after execution completes."""

        del args, kwargs
        if "FTP HYBRID BOOTSTRAP SUMMARY" in str(text):
            raise UnicodeEncodeError("ascii", "路径", 0, 1, "unsupported")

    code = run_bootstrap(
        config_path=path,
        yes=True,
        transport_factory=factory,
        print_fn=summary_pipe,
    )
    assert code == 1
