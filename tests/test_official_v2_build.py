"""official-v2 Docker/1Password Composer configuration and transaction fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from git_deploy.artifact_planner import ArtifactPlan
from git_deploy.combined_planner import CombinedPlanner
from git_deploy.config import load_config, select_remote
from git_deploy.build_cache import BuildCache, build_fingerprint, docker_runner_identity
from git_deploy.docker_runner import DockerBuildRunner, DockerCli
from git_deploy.errors import RemoteDriftError
from git_deploy.expected_state import ExpectedStateStore, FileEntry, build_expected_state
from git_deploy.git_store import PersistentGitStore
from git_deploy.models import ArtifactConfig, PlannedFile, ProjectConfig
from git_deploy.object_store import ContentAddressedStore
from git_deploy.state_executor import FakeRemotePath, InMemoryTransport, StateDeploymentExecutor
from git_deploy.state_planner import SourceDiffPlan
from git_deploy.state_rollback import StateRollbackService
from git_deploy.target_identity import policy_fingerprint_for_project, resolve_target_identity
from git_deploy.onepassword_runner import OnePasswordDockerCli


class ArtifactProcess:
    """Fake successful Docker run that creates vendor output in the mounted worktree."""

    returncode = 0

    def __init__(self, worktree: Path):
        """Retain the parsed mounted worktree."""

        self.worktree = worktree

    def communicate(self, input=None, timeout=None):
        """Create deterministic vendor bytes and return successful output."""

        del input, timeout
        output = self.worktree / "vendor/demo/package"
        output.mkdir(parents=True, exist_ok=True)
        (output / "lib.php").write_text("<?php // built\n")
        return b"built", b""

    def poll(self):
        """Return successful state."""

        return 0

    def terminate(self):
        """No-op fake termination."""

    def kill(self):
        """No-op fake kill."""


class ArtifactDockerCli(DockerCli):
    """Fake Docker CLI that never contacts a daemon or registry."""

    def __init__(self):
        """Initialize recorded calls."""

        self.calls: list[list[str]] = []

    def run(self, args, *, environment):
        """Resolve a fixed digest and accept cleanup."""

        del environment
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, b"sha256:official\n", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def start(self, args, *, environment):
        """Parse the sole mount and return an artifact-producing process."""

        del environment
        argv = list(args)
        self.calls.append(argv)
        mount = argv[argv.index("--mount") + 1]
        source = next(part[4:] for part in mount.split(",") if part.startswith("src="))
        return ArtifactProcess(Path(source))


def test_official_v2_docker_config_and_fake_cli(tmp_path: Path) -> None:
    """Sample keeps prod op refs isolated and fake Docker uses immutable image ID."""

    repository = tmp_path / "repo"
    repository.mkdir()
    example = Path(__file__).parents[1] / "git-deploy.example.toml"
    text = example.read_text(encoding="utf-8").replace('repository = "."', f'repository = "{repository}"')
    config_path = tmp_path / "example.toml"
    config_path.write_text(text, encoding="utf-8")
    config = load_config(config_path)
    dev = select_remote(config, "dev")[2]["official-v2"]
    prod = select_remote(config, "prod")[2]["official-v2"]
    assert dev.build is not None and dev.build.onepassword is None
    assert prod.build is not None and prod.build.onepassword is not None
    assert prod.build.docker is not None and "@sha256:" in prod.build.docker.image

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fake = ArtifactDockerCli()
    result = DockerBuildRunner(cli=fake).run(worktree, dev.build)
    assert result.runner == "docker"
    assert (worktree / "vendor/demo/package/lib.php").is_file()
    assert not any(call[0] == "pull" for call in fake.calls)
    run = next(call for call in fake.calls if call[0] == "run")
    assert "sha256:official" in run


def _vendor_fixture(tmp_path: Path, transport: InMemoryTransport | None = None):
    """Seed a trusted vendor current and return one artifact-only deployment."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "app").write_text("app")
    subprocess.run(["git", "-C", str(repo), "add", "app"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "app"], check=True)
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    project = ProjectConfig(
        "official-v2",
        repo,
        "/srv",
        artifacts=(ArtifactConfig("vendor", "vendor", "tree"),),
    )
    identity = resolve_target_identity({"protocol": "sftp", "host": "fake"}, project)
    root = tmp_path / "target"
    git_store = PersistentGitStore(root, repo)
    git_store.ensure_layout()
    git_store._publish_repository_identity()
    old = b"old-vendor"
    new = b"new-vendor"
    old_hash = hashlib.sha256(old).hexdigest()
    new_hash = hashlib.sha256(new).hexdigest()
    ContentAddressedStore(root).put(old)
    before_entry = FileEntry("vendor/lib.php", "artifact:vendor", old_hash)
    before = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id=tree,
        applied_transition_ids=("t0",),
        physical_fingerprint=identity.physical_fingerprint,
        policy_fingerprint=policy_fingerprint_for_project(project),
        files=(before_entry,),
        artifacts=({"build_fingerprint": "old"},),
    )
    store = ExpectedStateStore(root, identity)
    store.cas_advance(expected_generation=None, state=before)
    remote = transport or InMemoryTransport()
    remote.files["/srv/vendor/lib.php"] = FakeRemotePath(old)
    operation = PlannedFile(
        "upload", "vendor/lib.php", "/srv/vendor/lib.php", "vendor/lib.php", old_hash, new_hash, len(new)
    )
    source = SourceDiffPlan(tree, tree, (), (), ("t1",), ("t0", "t1"))
    artifact = ArtifactPlan(
        "ready",
        (operation,),
        (FileEntry("vendor/lib.php", "artifact:vendor", new_hash),),
        (("vendor/lib.php", new_hash),),
    )
    combined = CombinedPlanner().combine((before_entry,), source, artifact)
    executor = StateDeploymentExecutor(
        project,
        identity,
        root,
        transport=remote,
        content_provider=lambda _path: new,
    )
    return executor, remote, store, identity, root, source, combined, old, new


def test_official_v2_docker_vendor_drift_noop_failure_and_rollback(tmp_path: Path) -> None:
    """Vendor state uses the same drift/no-op/restore/latest-rollback semantics."""

    # Drift refuses before a transaction.
    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    fixture = _vendor_fixture(drift_root)
    executor, remote, _store, _identity, root, source, combined, _old, _new = fixture
    remote.files["/srv/vendor/lib.php"] = FakeRemotePath(b"third")
    with pytest.raises(RemoteDriftError):
        executor.deploy(source, combined.files, target_entries=combined.target_entries)
    assert not (root / "transactions").exists()

    # Success, repeated target no-op, and latest rollback.
    success_root = tmp_path / "success"
    success_root.mkdir()
    executor, remote, store, identity, root, source, combined, old, new = _vendor_fixture(success_root)
    result = executor.deploy(
        source,
        combined.files,
        target_entries=combined.target_entries,
        artifact_provenance=({"build_fingerprint": "new"},),
    )
    assert result["status"] == "succeeded"
    assert remote.files["/srv/vendor/lib.php"].data == new
    no_op_plan = SourceDiffPlan(source.after_tree_id, source.after_tree_id, (), (), (), source.applied_transition_ids, static_noop=True)
    assert executor.deploy(no_op_plan, ())["status"] == "already deployed"
    rolled = StateRollbackService(executor.project, identity, root, transport=remote).rollback_latest()
    assert rolled.status == "succeeded"
    assert remote.files["/srv/vendor/lib.php"].data == old
    assert store.load_current_state()[1].artifacts[0]["build_fingerprint"] == "old"  # type: ignore[index]

    # Corrupt write fails readback and restores old bytes/state.
    class CorruptOnce(InMemoryTransport):
        fired = False

        def write_file(self, remote_path, data, executable=False):
            if not self.fired:
                self.fired = True
                return super().write_file(remote_path, b"corrupt", executable)
            return super().write_file(remote_path, data, executable)

        def write_file_stream(self, remote_path, chunks, executable=False):
            return self.write_file(remote_path, b"".join(chunks), executable)

    failure_root = tmp_path / "failure"
    failure_root.mkdir()
    corrupt = CorruptOnce()
    executor, remote, store, _identity, _root, source, combined, old, _new = _vendor_fixture(
        failure_root, corrupt
    )
    result = executor.deploy(source, combined.files, target_entries=combined.target_entries)
    assert result["status"] == "restored"
    assert remote.files["/srv/vendor/lib.php"].data == old
    assert store.load_current_state()[1].generation == 1  # type: ignore[index]


def test_official_v2_onepassword_rotation_bypasses_cache_without_leaks(tmp_path: Path) -> None:
    """COMPOSER_AUTH reaches digest Docker via fake op; rotations rebuild and stay masked."""

    repository = tmp_path / "repo"
    repository.mkdir()
    example = Path(__file__).parents[1] / "git-deploy.example.toml"
    text = example.read_text(encoding="utf-8").replace(
        'repository = "."', f'repository = "{repository}"'
    )
    config_path = tmp_path / "example.toml"
    config_path.write_text(text, encoding="utf-8")
    prod = select_remote(load_config(config_path), "prod")[2]["official-v2"]
    assert prod.build is not None and prod.build.onepassword is not None
    assert prod.build.docker is not None

    docker_log = tmp_path / "docker-log.jsonl"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\n"
        "args=sys.argv[1:]\n"
        "if args[:2]==['image','inspect']: print('sha256:official'); raise SystemExit(0)\n"
        "if args and args[0]=='run':\n"
        "    assert os.environ['COMPOSER_AUTH']\n"
        "    assert not any(k.startswith('OP_') for k in os.environ)\n"
        f"    open({str(docker_log)!r},'a').write(json.dumps({{'argv':args,'names':sorted(os.environ)}})+'\\n')\n"
        "    print(os.environ['COMPOSER_AUTH'])\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    op = tmp_path / "op"
    op.write_text(
        f"#!{sys.executable}\n"
        "import os,subprocess,sys\n"
        "args=sys.argv[1:]; assert args[:2]==['run','--']\n"
        "secret=os.environ['OP_FAKE_SECRET']; env=os.environ.copy()\n"
        "for k,v in list(env.items()):\n"
        "    if v.startswith('op://'): env[k]=secret\n"
        "r=subprocess.run(args[2:],env=env,capture_output=True)\n"
        "sys.stdout.buffer.write(r.stdout.replace(secret.encode(),b'***'))\n"
        "sys.stderr.buffer.write(r.stderr.replace(secret.encode(),b'***'))\n"
        "raise SystemExit(r.returncode)\n",
        encoding="utf-8",
    )
    op.chmod(0o755)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outputs: list[str] = []
    for secret in ("ROTATED-ONE", "ROTATED-TWO"):
        source = {
            "PATH": os.environ.get("PATH", ""),
            "OP_FAKE_SECRET": secret,
            "OP_SERVICE_ACCOUNT_TOKEN": "AUTH-TOKEN",
        }
        cli = OnePasswordDockerCli(
            DockerCli(str(docker)),
            prod.build.onepassword,
            op_executable=str(op),
            source_environment=source,
        )
        result = DockerBuildRunner(cli=cli, source_environment=source).run(
            worktree, prod.build
        )
        outputs.append(result.commands[0].stdout.strip())
    assert outputs == ["***", "***"]
    logs = [json.loads(line) for line in docker_log.read_text().splitlines()]
    assert len(logs) == 2
    rendered = repr(logs) + repr(outputs)
    for sensitive in (
        "ROTATED-ONE",
        "ROTATED-TWO",
        "AUTH-TOKEN",
        "op://build/composer/auth",
    ):
        assert sensitive not in rendered
    assert all(not any(name.startswith("OP_") for name in row["names"]) for row in logs)

    fingerprint = build_fingerprint(
        source_tree_id="tree",
        build=prod.build,
        artifacts=prod.artifacts,
        runner_identity=docker_runner_identity(prod.build, "sha256:official"),
    )
    cache = BuildCache(tmp_path / "target")
    assert cache.lookup(fingerprint, secrets_enabled=True).hit is False
    assert "bypass" in cache.lookup(fingerprint, secrets_enabled=True).reason
