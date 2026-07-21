"""Minimal parser and zero-side-effect dry-run/build-failure tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import git_deploy.cli as cli
from git_deploy.doctor import DoctorResult
from git_deploy.errors import BuildError
from tests.conftest import write_config


def test_help_exposes_only_lite_workflow(capsys: pytest.CaptureFixture[str]) -> None:
    """Help names default deploy, build, doctor, and bootstrap without old state commands."""

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "build" in output
    assert "doctor" in output
    assert "bootstrap" in output
    assert "--probe-ftp-hybrid" in output
    assert "replace the" in output
    assert "local capability profile" in output
    assert "--force" in output
    assert "--no-create-root" in output
    assert "--reprobe" not in output
    assert "rollback" not in output


def test_bootstrap_cli_flags_and_rejects_deploy_only_options(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bootstrap accepts --yes/--force/--no-create-root and rejects deploy flags."""

    from git_deploy.config import load_config
    from tests.test_bootstrap import FakeBootstrapTransport, _ftp_hybrid_config, _valid_profile
    from git_deploy.ftp_hybrid import save_capability_profile
    import git_deploy.bootstrap as bootstrap_module

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
    monkeypatch.setenv("FTP_PROD", "secret")
    transport = FakeBootstrapTransport(load_config(path).target("prod"))

    def fake_probe(transport, target, runtime_base, *, now=None):  # noqa: ANN001, ANN202
        """Persist a profile for the CLI bootstrap path."""

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
    monkeypatch.setattr(
        bootstrap_module,
        "create_transport",
        lambda target: transport,
    )
    assert (
        cli.main(
            [
                "--config",
                str(path),
                "bootstrap",
                "prod",
                "--yes",
                "--force",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "FTP HYBRID BOOTSTRAP PLAN" in out
    assert "REPROBE" in out or "PROBE" in out or "READY" in out

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "bootstrap",
                "--dry-run",
            ]
        )
        == 2
    )


def test_init_and_doctor_parsing_unchanged_with_extra_nargs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init still rejects targets; doctor still accepts one target positional."""

    from git_deploy.doctor import DoctorResult

    assert cli.main(["init", "prod"]) == 2

    path = write_config(tmp_path)
    monkeypatch.setattr(
        cli,
        "run_doctor",
        lambda *a, **k: (DoctorResult("config", True, "ok"),),
    )
    # Non-git project doctor still returns after local checks.
    code = cli.main(["--config", str(path), "doctor", "dev"])
    assert code in {0, 1}


def test_dry_run_never_calls_deployer(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run computes a complete plan without opening a transport or writing state."""

    path = write_config(git_project)

    def forbidden(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Fail if dry-run reaches the stateful deployer."""

        raise AssertionError("execute_plan must not be called")

    monkeypatch.setattr(cli, "execute_prepared", forbidden)

    assert cli.main(["--config", str(path), "--dry-run", "--skip-build"]) == 0
    assert "Dry-run complete" in capsys.readouterr().out
    assert not (git_project / ".git/git-deploy/dev.json").exists()


def test_dry_run_reviews_after_deploy_without_remote_execution(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run prints the command but never reaches the stateful executor."""

    path = write_config(
        git_project,
        """
[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
after_deploy = ["sudo -n systemctl restart app.service"]
""",
    )

    def forbidden(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Reject any dry-run remote execution."""

        raise AssertionError("dry-run executed remote commands")

    monkeypatch.setattr(cli, "execute_prepared", forbidden)

    assert cli.main(["--config", str(path), "--dry-run", "--skip-build"]) == 0
    assert "AFTER  sudo -n systemctl restart app.service" in capsys.readouterr().out


def test_build_failure_prevents_deployer_call(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed build returns code 3 before planning or remote execution."""

    path = write_config(git_project)
    called = False

    def fail_build(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Simulate the first build step failing."""

        raise BuildError("boom")

    def record_deploy(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Record any forbidden attempt to reach remote execution."""

        nonlocal called
        called = True

    monkeypatch.setattr("git_deploy.prepared.run_build", fail_build)
    monkeypatch.setattr(cli, "execute_prepared", record_deploy)

    assert cli.main(["--config", str(path), "--yes"]) == 3
    assert not called


def test_build_only_does_not_read_git_or_state(tmp_path: Path) -> None:
    """The build command only executes configured steps even outside a Git repository."""

    path = write_config(
        tmp_path,
        """
[build]
steps = ["printf built > artifact"]

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )

    assert cli.main(["--config", str(path), "build"]) == 0
    assert (tmp_path / "artifact").read_text(encoding="utf-8") == "built"


def test_full_dry_run_recovers_from_corrupt_state(git_project: Path) -> None:
    """Explicit full mode can ignore corrupt old state and plan a safe complete rebuild."""

    path = write_config(git_project)
    state = git_project / ".git/git-deploy/dev.json"
    state.parent.mkdir(parents=True)
    state.write_text("not-json", encoding="utf-8")

    assert cli.main(["--config", str(path), "--full", "--dry-run", "--skip-build"]) == 0
    assert state.read_text(encoding="utf-8") == "not-json"


def test_confirmation_rejects_noninteractive_without_yes(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automation must opt into mutation with --yes instead of hanging for input."""

    path = write_config(git_project)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.main(["--config", str(path), "--skip-build"]) == 2


def test_ftp_hybrid_doctor_probe_requires_confirmation_and_discloses_scope(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only explicit Doctor probe plus confirmation may authorize protected writes."""

    aggregation = git_project / ".deploy/frontend-root"
    aggregation.mkdir(parents=True)
    (aggregation / "index.html").write_text("index\n", encoding="utf-8")
    path = write_config(
        git_project,
        """
project_id = "github.com/acme/ftp-probe"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.dev]
protocol = "ftp"
host = "ftp.example.invalid"
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
""",
    )
    called: list[bool] = []

    def diagnose(*args, probe_ftp_hybrid: bool = False, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Record whether the mutating probe authorization reached Doctor."""

        called.append(probe_ftp_hybrid)
        return (DoctorResult("FTP Hybrid Capability Probe", True, "supported"),)

    monkeypatch.setattr(cli, "run_doctor", diagnose)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert (
        cli.main(
            ["--config", str(path), "doctor", "dev", "--probe-ftp-hybrid"]
        )
        == 2
    )
    assert not called

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "doctor",
                "dev",
                "--probe-ftp-hybrid",
                "--yes",
            ]
        )
        == 0
    )
    assert called == [True]
    assert ".git-deploy/ftp-probe" in capsys.readouterr().out


def test_doctor_reports_non_git_instead_of_exiting_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor reaches its diagnostic renderer even when no Git metadata exists."""

    path = write_config(tmp_path)

    def diagnosed(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        """Return the Git failure that a real doctor would collect."""

        return (DoctorResult("git", False, "not a Git worktree"),)

    monkeypatch.setattr(cli, "run_doctor", diagnosed)

    assert cli.main(["--config", str(path), "doctor"]) == 1


def test_missing_output_after_successful_build_fails_before_remote(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful build that omits a required output cannot reach execution."""

    path = write_config(git_project, create_outputs=False)
    executed = False

    def record(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Record any forbidden deployment call."""

        nonlocal executed
        executed = True

    monkeypatch.setattr(cli, "execute_prepared", record)

    assert cli.main(["--config", str(path), "--yes"]) == 4
    assert not executed
    assert not (git_project / ".git/git-deploy/dev.json").exists()


def test_build_created_dirty_worktree_is_reported_and_can_be_required_clean(
    git_project: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Build-created worktree changes are visible and clean policy blocks them."""

    warning_config = write_config(
        git_project,
        """
[build]
steps = ["touch generated.txt"]

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )
    assert cli.main(["--config", str(warning_config), "--dry-run"]) == 0
    assert "build changed the worktree" in capsys.readouterr().out

    (git_project / "generated.txt").unlink()
    strict_config = write_config(
        git_project,
        """
[source]
require_clean_worktree = true

[build]
steps = ["touch generated.txt"]

[targets.dev]
protocol = "sftp"
host = "host"
username = "deploy"
remote_root = "/srv/app"
""",
    )
    assert cli.main(["--config", str(strict_config), "--dry-run"]) == 4


def test_init_creates_scaffold_without_existing_config(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The init command runs before config loading and never connects remotely."""

    monkeypatch.chdir(git_project)
    assert cli.main(["init"]) == 0
    assert (git_project / "deploy.toml").is_file()
    assert cli.main(["init"]) == 2


def test_create_root_is_rejected_outside_doctor(git_project: Path) -> None:
    """Deploy cannot silently accept a Doctor-only mutation option."""

    path = write_config(git_project)

    assert cli.main(["--config", str(path), "--create-root", "--dry-run"]) == 2
