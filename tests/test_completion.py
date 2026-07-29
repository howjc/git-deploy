"""Unit tests for shell Tab completion helpers and the completion CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

import git_deploy.cli as cli
from git_deploy import __version__
from git_deploy.completion import (
    CLI_OPTION_FLAGS,
    COMPLETION_SHELLS,
    FIXED_ACTIONS,
    completion_script_path,
    detect_login_shell,
    ensure_shell_completion_installed,
    install_shell_completion,
    list_action_completions,
    list_extra_completions,
    list_target_names,
    load_completion_script,
)
from tests.conftest import write_config


def test_list_target_names_from_project_config(tmp_path: Path) -> None:
    """Project deploy.toml target table keys become completion candidates."""

    config = write_config(
        tmp_path,
        """
default_target = "dev"

[targets.dev]
protocol = "sftp"
host = "dev.example"
username = "deploy"
remote_root = "/srv/dev"

[targets.prod]
protocol = "ftp"
host = "ftp.example"
username = "deploy"
password_env = "FTP_PASS"
remote_root = "/public_html"
""",
    )
    assert list_target_names(config=config) == ("dev", "prod")
    assert list_action_completions(config=config, prefix="p") == ["prod"]
    assert list_action_completions(config=config, prefix="b") == ["build", "bootstrap"]


def test_list_target_names_ambiguous_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both project and workspace files exist locally, target completion stays empty."""

    write_config(tmp_path)
    (tmp_path / "deploy.workspace.toml").write_text(
        'default_target = "dev"\n\n[[repositories]]\nname = "app"\npath = "."\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert list_target_names() == ()
    assert "build" in list_action_completions()
    assert "dev" not in list_action_completions()


def test_list_target_names_from_workspace_union(tmp_path: Path) -> None:
    """Workspace completion unions member deploy.toml targets and default_target."""

    api = tmp_path / "api"
    web = tmp_path / "web"
    api.mkdir()
    web.mkdir()
    write_config(
        api,
        """
[targets.dev]
protocol = "sftp"
host = "a"
username = "u"
remote_root = "/a"

[targets.staging]
protocol = "sftp"
host = "s"
username = "u"
remote_root = "/s"
""",
    )
    write_config(
        web,
        """
[targets.dev]
protocol = "sftp"
host = "b"
username = "u"
remote_root = "/b"

[targets.prod]
protocol = "sftp"
host = "c"
username = "u"
remote_root = "/c"
""",
    )
    workspace = tmp_path / "deploy.workspace.toml"
    workspace.write_text(
        """
default_target = "dev"

[[repositories]]
name = "api"
path = "api"

[[repositories]]
name = "web"
path = "web"
""",
        encoding="utf-8",
    )
    assert list_target_names(workspace=workspace) == ("dev", "prod", "staging")


def test_list_target_names_never_raises_on_corrupt_toml(tmp_path: Path) -> None:
    """Corrupt or unreadable TOML yields an empty target list instead of an error."""

    path = tmp_path / "deploy.toml"
    path.write_text("[[[not valid", encoding="utf-8")
    assert list_target_names(config=path) == ()


def test_extra_completions_for_completion_action() -> None:
    """After ``completion``, second-position words are shell kinds."""

    assert list_extra_completions("completion", prefix="") == list(COMPLETION_SHELLS)
    assert list_extra_completions("completion", prefix="z") == ["zsh"]
    assert list_extra_completions("completion", prefix="i") == ["install"]
    assert list_extra_completions("doctor", prefix="") == []


def test_detect_login_shell() -> None:
    """``$SHELL`` basenames map to supported completion shells."""

    assert detect_login_shell("/bin/bash") == "bash"
    assert detect_login_shell("/usr/bin/zsh") == "zsh"
    assert detect_login_shell("fish") is None


def test_install_shell_completion_writes_script_and_rc(tmp_path: Path) -> None:
    """Install writes the static script and an idempotent RC marker block."""

    first = install_shell_completion("bash", home=tmp_path)
    assert len(first) == 1
    assert first[0].script_written is True
    assert first[0].rc_updated is True
    script = completion_script_path("bash", home=tmp_path)
    assert script.is_file()
    assert "complete -F _git_deploy git-deploy" in script.read_text(encoding="utf-8")
    rc = (tmp_path / ".bashrc").read_text(encoding="utf-8")
    assert "# >>> git-deploy shell completion >>>" in rc
    assert str(script) in rc

    second = install_shell_completion("bash", home=tmp_path)
    assert second[0].already_current is True
    assert second[0].script_written is False
    assert second[0].rc_updated is False

    forced = install_shell_completion("bash", force=True, home=tmp_path)
    assert forced[0].script_written is True or forced[0].rc_updated is True
    assert (tmp_path / ".config/git-deploy/completion-install.version").read_text(
        encoding="utf-8"
    ).strip() == __version__


def test_install_shell_completion_zsh(tmp_path: Path) -> None:
    """Zsh install places ``_git-deploy`` and updates ``.zshrc`` fpath."""

    results = install_shell_completion("zsh", home=tmp_path)
    assert results[0].shell == "zsh"
    script = completion_script_path("zsh", home=tmp_path)
    assert script.name == "_git-deploy"
    rc = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert "fpath=" in rc
    assert str(script) in rc


def test_ensure_shell_completion_skips_when_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once-per-version ensure is a no-op when scripts already match this version."""

    monkeypatch.setenv("SHELL", "/bin/bash")
    assert ensure_shell_completion_installed(home=tmp_path) is not None
    assert ensure_shell_completion_installed(home=tmp_path) is None
    monkeypatch.setenv("GIT_DEPLOY_SKIP_COMPLETION_INSTALL", "1")
    assert ensure_shell_completion_installed(home=tmp_path) is None


def test_load_completion_scripts_are_packaged() -> None:
    """Static bash/zsh scripts ship with the package and mention key flags."""

    bash = load_completion_script("bash")
    zsh = load_completion_script("zsh")
    assert "complete -F _git_deploy git-deploy" in bash
    assert "compdef _git_deploy git-deploy" in zsh
    for flag in ("--dry-run", "--config", "--workspace", "--probe-ftp-hybrid"):
        assert flag in bash
        assert flag in zsh
    for action in FIXED_ACTIONS:
        assert action in bash
        assert action in zsh
    assert all(flag.startswith("--") for flag in CLI_OPTION_FLAGS if flag != "--help")


def test_cli_completion_bash_and_targets(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI prints static scripts and target names without loading remotes."""

    write_config(
        git_project,
        """
[targets.dev]
protocol = "sftp"
host = "dev.example"
username = "deploy"
remote_root = "/srv/dev"

[targets.prod]
protocol = "sftp"
host = "prod.example"
username = "deploy"
remote_root = "/srv/prod"
""",
    )
    monkeypatch.chdir(git_project)

    assert cli.main(["completion", "bash"]) == 0
    assert "complete -F _git_deploy git-deploy" in capsys.readouterr().out

    assert cli.main(["completion", "zsh"]) == 0
    assert "compdef _git_deploy git-deploy" in capsys.readouterr().out

    assert cli.main(["completion", "targets"]) == 0
    assert capsys.readouterr().out.splitlines() == ["dev", "prod"]

    assert cli.main(["--config", str(git_project / "deploy.toml"), "completion", "targets"]) == 0
    assert capsys.readouterr().out.splitlines() == ["dev", "prod"]


def test_cli_completion_rejects_bad_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Completion requires one kind and rejects deploy-only flags."""

    assert cli.main(["completion"]) != 0
    assert "usage: git-deploy completion" in capsys.readouterr().err

    assert cli.main(["completion", "bash", "--dry-run"]) != 0
    assert "does not accept" in capsys.readouterr().err

    assert cli.main(["completion", "bash", "--force"]) != 0
    assert "--force is only valid" in capsys.readouterr().err

    assert cli.main(["completion", "fish"]) != 0


def test_cli_completion_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI install detects the shell and reports written paths."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    # Path.home() may not honor HOME on all platforms; also pass via expanduser.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert cli.main(["completion", "install"]) == 0
    out = capsys.readouterr().out
    assert "bash:" in out
    assert (tmp_path / ".local/share/bash-completion/completions/git-deploy").is_file()
    assert "# >>> git-deploy shell completion >>>" in (tmp_path / ".bashrc").read_text(
        encoding="utf-8"
    )


def test_help_mentions_completion(capsys: pytest.CaptureFixture[str]) -> None:
    """Help text advertises the completion action."""

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert "completion" in capsys.readouterr().out
