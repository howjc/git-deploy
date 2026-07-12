"""1Password host/Docker wrapper and secret-boundary tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from git_deploy.build_runner import BuildExecutionError
from git_deploy.docker_runner import DockerBuildRunner, DockerCli
from git_deploy.models import BuildConfig, DockerBuildConfig, OnePasswordConfig
from git_deploy.onepassword_runner import OnePasswordDockerCli, OnePasswordHostRunner


def _fake_op(tmp_path: Path) -> tuple[Path, Path]:
    """Create a masking fake `op run` and return executable/log paths."""

    executable = tmp_path / "op"
    log = tmp_path / "op-log.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,subprocess,sys\n"
        "args=sys.argv[1:]\n"
        "assert args[:2] == ['run','--']\n"
        "env=os.environ.copy()\n"
        "for name,value in list(env.items()):\n"
        "    if value.startswith('op://'): env[name]='SENTINEL-SECRET'\n"
        "log=env.get('OP_FAKE_LOG')\n"
        "if log:\n"
        "    open(log,'w').write(json.dumps({'argv':args,'op_names':sorted(k for k in env if k.startswith('OP_'))}))\n"
        "result=subprocess.run(args[2:],env=env,capture_output=True)\n"
        "sys.stdout.buffer.write(result.stdout.replace(b'SENTINEL-SECRET',b'***'))\n"
        "sys.stderr.buffer.write(result.stderr.replace(b'SENTINEL-SECRET',b'***'))\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def _host_config(command: tuple[str, ...]) -> BuildConfig:
    """Return one secret-enabled host build config."""

    return BuildConfig(
        runner="host",
        commands=(command,),
        env_allowlist=("TOKEN",),
        onepassword=OnePasswordConfig((("TOKEN", "op://vault/item/field"),)),
    )


def test_host_op_run_fixed_chain_strips_op_auth_and_masks_output(tmp_path: Path) -> None:
    """Host command sees declared secret only; argv/log/result contain no sensitive values."""

    op, log = _fake_op(tmp_path)
    worktree = tmp_path / "tree"
    worktree.mkdir()
    code = (
        "import os,pathlib; "
        "assert os.environ['TOKEN']; "
        "assert not any(k.startswith('OP_') for k in os.environ); "
        "pathlib.Path('artifact').write_text(os.environ['TOKEN']); "
        "print(os.environ['TOKEN'])"
    )
    source = {
        "PATH": os.environ.get("PATH", ""),
        "OP_SERVICE_ACCOUNT_TOKEN": "AUTH-TOKEN",
        "OP_FAKE_LOG": str(log),
        "UNRELATED": "must-not-enter",
    }
    result = OnePasswordHostRunner(
        op_executable=str(op), source_environment=source
    ).run(worktree, _host_config((sys.executable, "-c", code)))
    assert result.commands[0].stdout.strip() == "***"
    assert (worktree / "artifact").read_text() == "SENTINEL-SECRET"
    logged = json.loads(log.read_text())
    rendered = repr(logged)
    assert logged["argv"][:2] == ["run", "--"]
    assert "--no-masking" not in rendered
    assert "op://" not in rendered
    assert "AUTH-TOKEN" not in rendered
    assert "SENTINEL-SECRET" not in rendered
    assert "must-not-enter" not in rendered


def test_host_op_missing_and_nonzero_are_structured_without_reference(tmp_path: Path) -> None:
    """Missing CLI and command failure occur locally with redacted diagnostics."""

    worktree = tmp_path / "tree"
    worktree.mkdir()
    runner = OnePasswordHostRunner(
        op_executable=str(tmp_path / "missing-op"), source_environment={"PATH": "/bin"}
    )
    with pytest.raises(BuildExecutionError) as missing:
        runner.run(worktree, _host_config((sys.executable, "-c", "pass")))
    assert missing.value.phase == "op_start"
    assert "op://" not in str(missing.value)

    op, _log = _fake_op(tmp_path)
    runner = OnePasswordHostRunner(
        op_executable=str(op), source_environment={"PATH": os.environ.get("PATH", "")}
    )
    with pytest.raises(BuildExecutionError) as nonzero:
        runner.run(
            worktree,
            _host_config((sys.executable, "-c", "import sys; sys.exit(17)")),
        )
    assert nonzero.value.phase == "op_nonzero"
    assert nonzero.value.returncode == 17
    assert "op://" not in str(nonzero.value)


def test_docker_env_op_wraps_only_run_and_strips_all_op_auth(tmp_path: Path) -> None:
    """The op→scrubber→docker chain passes names/values without OP_* or argv secrets."""

    op, _op_log = _fake_op(tmp_path)
    docker = tmp_path / "docker"
    docker_log = tmp_path / "docker-log.json"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "args=sys.argv[1:]\n"
        "if args[:2] == ['image','inspect']:\n"
        "    print('sha256:immutable'); raise SystemExit(0)\n"
        "if args and args[0] == 'run':\n"
        "    assert os.environ['TOKEN']=='SENTINEL-SECRET'\n"
        "    assert not any(k.startswith('OP_') for k in os.environ)\n"
        f"    open({str(docker_log)!r},'w').write(json.dumps({{'argv':args,'env_names':sorted(os.environ)}}))\n"
        "    print(os.environ['TOKEN']); raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    onepassword = OnePasswordConfig((("TOKEN", "op://vault/item/token"),))
    source = {
        "PATH": os.environ.get("PATH", ""),
        "OP_SERVICE_ACCOUNT_TOKEN": "AUTH-TOKEN",
    }
    cli = OnePasswordDockerCli(
        DockerCli(str(docker)),
        onepassword,
        op_executable=str(op),
        source_environment=source,
    )
    config = BuildConfig(
        runner="docker",
        commands=(("tool", "build"),),
        env_allowlist=("TOKEN",),
        docker=DockerBuildConfig("image@sha256:configured"),
        onepassword=onepassword,
    )
    worktree = tmp_path / "tree"
    worktree.mkdir()
    result = DockerBuildRunner(cli=cli, source_environment=source).run(worktree, config)
    assert "SENTINEL-SECRET" not in result.commands[0].stdout
    assert result.commands[0].stdout.strip() == "***"
    logged = json.loads(docker_log.read_text())
    rendered = repr(logged)
    assert "--env" in logged["argv"] and "TOKEN" in logged["argv"]
    assert "op://" not in rendered
    assert "AUTH-TOKEN" not in rendered
    assert "SENTINEL-SECRET" not in rendered
    assert not any(name.startswith("OP_") for name in logged["env_names"])


def test_docker_metadata_output_masking_and_container_cleanup(tmp_path: Path) -> None:
    """Secret output is masked and the named container is removed after completion."""

    op, _op_log = _fake_op(tmp_path)
    log = tmp_path / "calls.jsonl"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        f"open({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1:3] == ['image','inspect']: print('sha256:immutable')\n"
        "elif sys.argv[1] == 'run': print(os.environ['TOKEN'])\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    onepassword = OnePasswordConfig((("TOKEN", "op://vault/item/token"),))
    source = {"PATH": os.environ.get("PATH", ""), "OP_SESSION_TEST": "AUTH"}
    cli = OnePasswordDockerCli(
        DockerCli(str(docker)), onepassword, op_executable=str(op), source_environment=source
    )
    config = BuildConfig(
        runner="docker",
        commands=(("tool",),),
        env_allowlist=("TOKEN",),
        docker=DockerBuildConfig("image@sha256:configured"),
        onepassword=onepassword,
    )
    worktree = tmp_path / "tree"
    worktree.mkdir()
    result = DockerBuildRunner(cli=cli, source_environment=source).run(worktree, config)
    assert result.commands[0].stdout.strip() == "***"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(call[0] == "rm" for call in calls)
    assert "SENTINEL-SECRET" not in repr(result)
