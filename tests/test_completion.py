"""Unit tests for shell Tab completion helpers and the completion CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
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
from git_deploy.errors import ConfigError
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
    body = script.read_text(encoding="utf-8")
    assert body.startswith("#compdef git-deploy\n")
    rc = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert "fpath=" in rc
    assert str(script) in rc


def test_install_rc_quotes_special_home_paths(tmp_path: Path) -> None:
    """RC snippets shell-quote script paths that contain spaces."""

    home = tmp_path / "user home"
    home.mkdir()
    install_shell_completion("bash", home=home)
    rc = (home / ".bashrc").read_text(encoding="utf-8")
    script = completion_script_path("bash", home=home)
    # shlex.quote produces single-quoted paths for spaces.
    assert "source " in rc
    assert str(script) in rc or f"'{script}'" in rc


def test_ensure_shell_completion_skips_when_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure writes scripts once, never touches RC, then becomes a no-op."""

    monkeypatch.setenv("SHELL", "/bin/bash")
    assert ensure_shell_completion_installed(home=tmp_path) is not None
    assert completion_script_path("bash", home=tmp_path).is_file()
    # Ordinary CLI entry must not rewrite shell RC files.
    assert not (tmp_path / ".bashrc").exists()
    assert ensure_shell_completion_installed(home=tmp_path) is None
    monkeypatch.setenv("GIT_DEPLOY_SKIP_COMPLETION_INSTALL", "1")
    assert ensure_shell_completion_installed(home=tmp_path) is None


def test_ensure_shell_completion_never_updates_existing_rc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When scripts are missing, ensure still leaves a pre-existing RC alone."""

    monkeypatch.setenv("SHELL", "/bin/bash")
    rc = tmp_path / ".bashrc"
    rc.write_text("# user rc\n", encoding="utf-8")
    assert ensure_shell_completion_installed(home=tmp_path) is not None
    assert rc.read_text(encoding="utf-8") == "# user rc\n"
    assert "# >>> git-deploy shell completion >>>" not in rc.read_text(encoding="utf-8")


def test_ensure_shell_completion_refreshes_stale_script_without_rc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stale installed script body is rewritten; matching content is a no-op; no RC."""

    monkeypatch.setenv("SHELL", "/bin/bash")
    script = completion_script_path("bash", home=tmp_path)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stale v1.8.0 completion body\n", encoding="utf-8")
    rc = tmp_path / ".bashrc"
    rc.write_text("# user rc untouched\n", encoding="utf-8")

    results = ensure_shell_completion_installed(home=tmp_path)
    assert results is not None
    assert any(item.script_written for item in results)
    packaged = load_completion_script("bash")
    assert script.read_text(encoding="utf-8") == packaged
    assert rc.read_text(encoding="utf-8") == "# user rc untouched\n"
    err = capsys.readouterr().err
    assert "refreshed" in err
    assert "completion install" in err

    # Identical packaged content skips further writes and notes.
    assert ensure_shell_completion_installed(home=tmp_path) is None
    assert script.read_text(encoding="utf-8") == packaged
    assert rc.read_text(encoding="utf-8") == "# user rc untouched\n"
    assert capsys.readouterr().err == ""


def test_load_completion_scripts_are_packaged() -> None:
    """Static bash/zsh scripts ship with the package and mention key flags."""

    bash = load_completion_script("bash")
    zsh = load_completion_script("zsh")
    assert "complete -F _git_deploy git-deploy" in bash
    assert zsh.startswith("#compdef git-deploy\n")
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


def test_list_target_names_filters_unsafe_toml_keys(tmp_path: Path) -> None:
    """Raw TOML reader applies the same safe-name rule as full config load."""

    config = tmp_path / "deploy.toml"
    config.write_text(
        textwrap.dedent(
            """
            [targets.dev]
            protocol = "sftp"
            host = "dev.example"
            username = "deploy"
            remote_root = "/srv/dev"

            [targets."$(touch completion-proof)"]
            protocol = "sftp"
            host = "evil.example"
            username = "deploy"
            remote_root = "/srv/evil"

            [targets."`touch completion-proof-2`"]
            protocol = "sftp"
            host = "evil2.example"
            username = "deploy"
            remote_root = "/srv/evil2"

            [targets."has space"]
            protocol = "sftp"
            host = "sp.example"
            username = "deploy"
            remote_root = "/srv/sp"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    assert list_target_names(config=config) == ("dev",)


def test_bash_script_never_uses_compgen_w_for_dynamic_targets() -> None:
    """Shipped bash completion must not feed dynamic targets into ``compgen -W``."""

    bash = load_completion_script("bash")
    assert "compgen -W" in bash  # fixed actions/flags may still use it
    assert "mapfile -t targets" in bash
    # Dynamic targets join COMPREPLY by prefix match, not via word-list expansion.
    assert 'COMPREPLY+=("$candidate")' in bash
    assert 'compgen -W "$actions $targets"' not in bash
    assert 'compgen -W "$targets"' not in bash


def test_zsh_script_uses_line_array_for_targets() -> None:
    """Zsh loads targets as a line array instead of word-splitting a string."""

    zsh = load_completion_script("zsh")
    assert 'targets=("${(@f)$(' in zsh
    assert "t=(${=targets})" not in zsh
    assert "first+=(${=targets})" not in zsh


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bash_completion_does_not_execute_malicious_target_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real bash completion must not run command substitution from target text.

    Even if a raw malicious key reached the shell (defense in depth), the
    completion function must not re-expand it. This test forces the target
    helper to emit a marker payload and asserts the marker file is never created.
    """

    marker = tmp_path / "completion-proof"
    marker2 = tmp_path / "completion-proof-2"
    assert not marker.exists()
    assert not marker2.exists()

    # Stub ``git-deploy completion targets`` so the shell receives hostile text
    # without depending on TOML allowing the key after the config filter.
    stub = tmp_path / "git-deploy"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            if [[ "$1" == "completion" && "$2" == "targets" ]]; then
              printf '%s\\n' '$(touch {marker})' '`touch {marker2}`' 'dev'
              exit 0
            fi
            exit 1
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    script = load_completion_script("bash")
    script_path = tmp_path / "git-deploy.bash"
    script_path.write_text(script, encoding="utf-8")

    probe = textwrap.dedent(
        f"""\
        set -euo pipefail
        source {script_path}
        COMP_WORDS=(git-deploy "")
        COMP_CWORD=1
        COMP_LINE='git-deploy '
        COMP_POINT=${{#COMP_LINE}}
        _git_deploy
        # Print replies for diagnostics (should include safe 'dev' if any).
        printf '%s\\n' "${{COMPREPLY[@]-}}"
        """
    )
    result = subprocess.run(
        ["bash", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "bash completion executed $(touch …) from target text"
    assert not marker2.exists(), "bash completion executed backtick target text"


def test_install_preserves_rc_symlink(
    tmp_path: Path,
) -> None:
    """Atomic RC install follows a symlink and never replaces the link node."""

    real_rc = tmp_path / "dotfiles" / "bashrc"
    real_rc.parent.mkdir(parents=True)
    real_rc.write_text("# user bashrc\n", encoding="utf-8")
    link = tmp_path / ".bashrc"
    link.symlink_to(real_rc)

    results = install_shell_completion("bash", home=tmp_path)
    assert results[0].rc_updated is True
    assert link.is_symlink()
    assert link.resolve() == real_rc.resolve()
    body = real_rc.read_text(encoding="utf-8")
    assert "# >>> git-deploy shell completion >>>" in body
    assert "# user bashrc" in body


def test_install_preserves_zsh_rc_symlink(tmp_path: Path) -> None:
    """Zsh RC symlink install updates the target file, not the link node."""

    real_rc = tmp_path / "dotfiles" / "zshrc"
    real_rc.parent.mkdir(parents=True)
    real_rc.write_text("# user zshrc\n", encoding="utf-8")
    link = tmp_path / ".zshrc"
    link.symlink_to(real_rc)

    results = install_shell_completion("zsh", home=tmp_path)
    assert results[0].rc_updated is True
    assert link.is_symlink()
    assert "# >>> git-deploy shell completion >>>" in real_rc.read_text(encoding="utf-8")


def test_install_rejects_dangling_rc_symlink(tmp_path: Path) -> None:
    """Dangling RC symlink fails with a clear error instead of replacing the link."""

    link = tmp_path / ".bashrc"
    link.symlink_to(tmp_path / "missing-bashrc")
    with pytest.raises(ConfigError, match="dangling|unresolvable symlink"):
        install_shell_completion("bash", home=tmp_path)
    assert link.is_symlink()
