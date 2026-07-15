"""Configuration discovery and path-resolution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.config import discover_config, load_config, select_remote
from git_deploy.errors import ConfigurationError
from git_deploy.remote_permissions import load_sftp_permission_policy
from git_deploy.state import DeploymentStore


def test_current_directory_deploy_toml_has_default_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use ``./deploy.toml`` before an environment-level fallback."""

    current = tmp_path / "current"
    fallback = tmp_path / "fallback.toml"
    current.mkdir()
    (current / "deploy.toml").write_text("[server]\n[projects.demo]\n", encoding="utf-8")
    fallback.write_text("[server]\n[projects.demo]\n", encoding="utf-8")
    monkeypatch.chdir(current)
    monkeypatch.setenv("GIT_DEPLOY_CONFIG", str(fallback))

    assert discover_config() == (current / "deploy.toml").resolve()


def test_load_config_resolves_project_paths_from_toml_directory(tmp_path: Path) -> None:
    """Resolve repository and local state paths relative to the TOML file."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        """
[server]
protocol = "sftp"

[projects.demo]
repository = "repository"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
include = ["src/**"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.projects["demo"].repository == repository.resolve()
    assert config.projects["demo"].local_state_dir == (tmp_path / ".state/demo").resolve()
    assert config.projects["demo"].include == ("src/**",)


def test_sftp_permission_policy_accepts_owner_group_and_octal_modes(
    tmp_path: Path,
) -> None:
    """Validate configurable ownership while retaining safe SFTP mode defaults."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
owner = "www-data"
group = "web"
file_mode = "0640"
executable_mode = 0o750
directory_mode = "0750"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    policy = load_sftp_permission_policy(config.remotes["default"].values)

    assert policy.owner == "www-data"
    assert policy.group == "web"
    assert policy.file_mode == 0o640
    assert policy.executable_mode == 0o750
    assert policy.directory_mode == 0o750


def test_sftp_permission_policy_defaults_and_invalid_protocol_are_fail_closed(
    tmp_path: Path,
) -> None:
    """Use 0644/0755 defaults and reject POSIX policy on FTP transports."""

    defaults = load_sftp_permission_policy({"protocol": "sftp"})
    assert defaults.file_mode == 0o644
    assert defaults.executable_mode == 0o755
    assert defaults.directory_mode == 0o755

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "ftps"
file_mode = "0644"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="cannot guarantee POSIX"):
        load_config(config_path)


def test_sftp_permission_policy_rejects_decimal_looking_mode(tmp_path: Path) -> None:
    """Reject decimal 644 so an accidental TOML representation is not misapplied."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
file_mode = 644

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="octal string"):
        load_config(config_path)


def test_sftp_ownership_rejects_shell_metacharacters() -> None:
    """Reject unsafe ownership identities before opening an SSH connection."""

    with pytest.raises(ConfigurationError, match="safe user/group"):
        load_sftp_permission_policy(
            {"protocol": "sftp", "owner": "www-data; touch /tmp/unsafe"}
        )


def test_explicit_missing_config_is_rejected(tmp_path: Path) -> None:
    """Do not silently fall back when an explicit path is invalid."""

    with pytest.raises(ConfigurationError, match="does not exist"):
        discover_config(str(tmp_path / "missing.toml"))


def test_named_remotes_apply_project_overrides_and_isolate_state(tmp_path: Path) -> None:
    """Resolve dev/prod roots and hooks without sharing deployment history."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        """
[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"

[projects.demo]
repository = "repository"
local_state_dir = ".state/demo"
post_commands = ["shared-command"]

[projects.demo.remotes.dev]
remote_root = "/srv/dev/demo"
post_commands = []
health_urls = ["https://dev.example.invalid/health"]

[projects.demo.remotes.prod]
remote_root = "/srv/prod/demo"
post_commands = ["restart-production"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    dev_name, dev_server, dev_projects = select_remote(config, "dev")
    prod_name, prod_server, prod_projects = select_remote(config, "prod")
    dev = dev_projects["demo"]
    prod = prod_projects["demo"]

    assert dev_name == "dev"
    assert prod_name == "prod"
    assert dev_server.values["host"] == "dev.example.invalid"
    assert prod_server.values["host"] == "prod.example.invalid"
    assert dev.remote_root == "/srv/dev/demo"
    assert prod.remote_root == "/srv/prod/demo"
    assert dev.post_commands == ()
    assert prod.post_commands == ("restart-production",)
    assert dev.health_urls == ("https://dev.example.invalid/health",)
    assert DeploymentStore(dev).root == tmp_path / ".state/demo/remotes/dev"
    assert DeploymentStore(prod).root == tmp_path / ".state/demo/remotes/prod"


def test_multiple_named_remotes_require_an_explicit_selection(tmp_path: Path) -> None:
    """Fail closed when omitting the remote could accidentally select production."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"

[remotes.prod]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    with pytest.raises(ConfigurationError, match="--remote is required"):
        select_remote(config, None)


def test_legacy_server_configuration_resolves_as_default_remote(tmp_path: Path) -> None:
    """Keep existing single-server configuration and state paths compatible."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
local_state_dir = ".state/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    remote_name, _, projects = select_remote(config, None)
    project = projects["demo"]

    assert remote_name == "default"
    assert project.remote == "default"
    assert DeploymentStore(project).root == tmp_path / ".state/demo"


def test_target_id_remote_identity_shared_and_isolated(tmp_path: Path) -> None:
    """Named remotes map to physical target_id; username/alias excluded from payload."""

    from git_deploy.config import resolve_project_target
    from git_deploy.target_identity import build_physical_payload, resolve_target_identity

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "App.Example.COM."
username = "dev-user"
port = 22

[remotes.prod]
protocol = "sftp"
host = "app.example.com"
username = "prod-user"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo//"
target_id = "shared-demo"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    _, dev_server, dev_projects = select_remote(config, "dev")
    _, prod_server, prod_projects = select_remote(config, "prod")

    dev_id = resolve_project_target(dev_server, dev_projects["demo"])
    prod_id = resolve_project_target(prod_server, prod_projects["demo"])
    # Same canonical payload + explicit id → shared physical target.
    assert dev_id.target_id == "shared-demo"
    assert prod_id.target_id == "shared-demo"
    assert dev_id.physical_fingerprint == prod_id.physical_fingerprint
    assert dev_projects["demo"].remote_root == "/srv/demo"

    # Different protocol with the same explicit id is rejected at load time.
    other_payload = build_physical_payload(
        protocol="ftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/demo",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical"):
        resolve_target_identity(
            {"protocol": "ftp", "host": "app.example.com"},
            "demo",
            remote_root="/srv/demo",
            explicit_target_id="shared-demo",
            bound_payload=dev_id.payload,
        )
    assert other_payload.fingerprint() != dev_id.physical_fingerprint


def test_remote_identity_without_explicit_id_derives_from_payload(tmp_path: Path) -> None:
    """Without target_id, different roots yield different derived ids."""

    from git_deploy.config import resolve_project_target

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "h.example"

[remotes.prod]
protocol = "sftp"
host = "h.example"

[projects.demo]
repository = "{repository}"
local_state_dir = ".state/demo"

[projects.demo.remotes.dev]
remote_root = "/srv/dev"

[projects.demo.remotes.prod]
remote_root = "/srv/prod"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    _, dev_server, dev_projects = select_remote(config, "dev")
    _, prod_server, prod_projects = select_remote(config, "prod")
    dev_id = resolve_project_target(dev_server, dev_projects["demo"])
    prod_id = resolve_project_target(prod_server, prod_projects["demo"])
    assert dev_id.target_id != prod_id.target_id
    assert dev_id.state_root(tmp_path / ".state/demo").name == dev_id.target_id


def test_explicit_target_id_collision_rejected_on_load(tmp_path: Path) -> None:
    """Real deploy.toml load rejects distinct physical payloads sharing an explicit id.

    Must fail before any remote connect or state-dir access.
    """

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "dev.example"

[remotes.prod]
protocol = "sftp"
host = "prod.example"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
target_id = "forced-shared"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical"):
        load_config(config_path)
    # No state directory access should have been required.
    assert not (tmp_path / ".state").exists()


def test_explicit_target_id_collision_different_roots_on_load(tmp_path: Path) -> None:
    """Same host but different remote_root with one explicit id fails at load."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "app.example"

[remotes.prod]
protocol = "sftp"
host = "app.example"

[projects.demo]
repository = "{repository}"
target_id = "forced-shared"

[projects.demo.remotes.dev]
remote_root = "/srv/dev"

[projects.demo.remotes.prod]
remote_root = "/srv/prod"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical"):
        load_config(config_path)


def test_ftps_effective_port_matches_transport_default() -> None:
    """FTPS identity default effective port equals transport connect default (21)."""

    from git_deploy.target_identity import (
        build_physical_payload,
        default_port_for_protocol,
        effective_port,
    )

    assert default_port_for_protocol("ftps") == 21
    assert effective_port("ftps", None) == 21
    payload = build_physical_payload(
        protocol="ftps",
        host="ftp.example",
        project="demo",
        remote_root="/srv",
    )
    assert payload.port == 21
    # Explicit override still works.
    assert effective_port("ftps", 990) == 990


def test_host_build_and_artifact_schema(tmp_path: Path) -> None:
    """Host build defaults and file/tree artifact mappings resolve immutably."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"

[projects.demo.build]
commands = [["composer", "install"], ["npm", "run", "build"]]
timeout = 321
cwd = "frontend"
env_allowlist = ["CI", "COMPOSER_AUTH"]

[[projects.demo.artifacts]]
source = "vendor"
destination = "vendor"
kind = "tree"

[[projects.demo.artifacts]]
source = "bin/server"
destination = "bin/server"
kind = "file"
""".strip(),
        encoding="utf-8",
    )

    project = load_config(config_path).projects["demo"]
    assert project.build is not None
    assert project.build.runner == "host"
    assert project.build.commands == (
        ("composer", "install"),
        ("npm", "run", "build"),
    )
    assert project.build.timeout == 321
    assert project.build.cwd == "frontend"
    assert project.build.env_allowlist == ("CI", "COMPOSER_AUTH")
    assert [(item.source, item.destination, item.kind) for item in project.artifacts] == [
        ("vendor", "vendor", "tree"),
        ("bin/server", "bin/server", "file"),
    ]


@pytest.mark.parametrize(
    ("build_body", "message"),
    [
        ('commands = ["npm run build"]', "argv array"),
        ('commands = [["npm"]]\ntimeout = 0', "positive integer"),
        ('commands = [["npm"]]\ncwd = "../outside"', "traversal"),
        ('commands = [["npm"]]\nenv_allowlist = ["BAD-NAME"]', "invalid name"),
        ('commands = [["npm"]]\nextra_args = ["--privileged"]', "unsupported keys"),
    ],
)
def test_host_build_rejects_invalid_contract(
    tmp_path: Path, build_body: str, message: str
) -> None:
    """Host build rejects shell strings, unsafe paths/env, and unknown knobs."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[projects.demo.build]
{build_body}
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match=message):
        load_config(config_path)


@pytest.mark.parametrize(
    ("artifact_body", "message"),
    [
        ('source = "../vendor"\ndestination = "vendor"\nkind = "tree"', "traversal"),
        ('source = "vendor"\ndestination = "/srv/vendor"\nkind = "tree"', "relative"),
        ('source = "vendor"\ndestination = "vendor"\nkind = "link"', "file.*tree"),
    ],
)
def test_artifact_schema_rejects_unsafe_paths_and_kinds(
    tmp_path: Path, artifact_body: str, message: str
) -> None:
    """Artifact mappings remain worktree/remote-root relative and regular-only."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[[projects.demo.artifacts]]
{artifact_body}
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match=message):
        load_config(config_path)


def test_docker_build_schema_defaults_and_rejects_arbitrary_args(tmp_path: Path) -> None:
    """Docker config has closed network/pull enums and no raw run arguments."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[projects.demo.build]
runner = "docker"
commands = [["composer", "install"]]
[projects.demo.build.docker]
image = "composer:2"
""".strip(),
        encoding="utf-8",
    )
    build = load_config(config_path).projects["demo"].build
    assert build is not None and build.docker is not None
    assert build.docker.platform == "linux/amd64"
    assert build.docker.network == "none"
    assert build.docker.pull_policy == "never"

    with config_path.open("a", encoding="utf-8") as handle:
        handle.write('\nrun_args = ["--privileged"]\n')
    with pytest.raises(ConfigurationError, match="unsupported keys"):
        load_config(config_path)


def test_onepassword_schema_accepts_only_allowlisted_op_references(tmp_path: Path) -> None:
    """1Password config stores opaque references under declared build names only."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    base = f"""
[server]
protocol = "sftp"
host = "build.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[projects.demo.build]
commands = [["composer", "install"]]
env_allowlist = ["COMPOSER_AUTH"]
[projects.demo.build.onepassword.env]
COMPOSER_AUTH = "op://build/composer/auth"
""".strip()
    config_path.write_text(base, encoding="utf-8")
    build = load_config(config_path).projects["demo"].build
    assert build is not None and build.onepassword is not None
    assert build.onepassword.as_dict() == {
        "COMPOSER_AUTH": "op://build/composer/auth"
    }
    from git_deploy.config import build_config_summary

    summary = build_config_summary(load_config(config_path).projects["demo"])
    assert summary["secret_provider"] == "1password"
    assert summary["secret_env_names"] == ["COMPOSER_AUTH"]
    assert "op://" not in repr(summary)

    config_path.write_text(base.replace("op://build/composer/auth", "plaintext"), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="op://"):
        load_config(config_path)
    config_path.write_text(base.replace("COMPOSER_AUTH =", "OP_SESSION_X ="), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid or reserved"):
        load_config(config_path)


def test_remote_host_build_override_replaces_whole_project_config(tmp_path: Path) -> None:
    """Remote host build/artifacts replace defaults and report their source layer."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "dev.example"
[remotes.prod]
protocol = "sftp"
host = "prod.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[projects.demo.build]
commands = [["build-prod"]]
timeout = 900
[[projects.demo.artifacts]]
source = "dist-prod"
destination = "dist"
kind = "tree"
[projects.demo.remotes.dev.build]
commands = [["build-dev"]]
timeout = 30
[[projects.demo.remotes.dev.artifacts]]
source = "dist-dev"
destination = "preview"
kind = "tree"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    dev = select_remote(config, "dev")[2]["demo"]
    prod = select_remote(config, "prod")[2]["demo"]
    assert dev.build is not None and dev.build.commands == (("build-dev",),)
    assert dev.build.timeout == 30
    assert [item.destination for item in dev.artifacts] == ["preview"]
    assert dev.build_origin == "remote:dev"
    assert dev.artifacts_origin == "remote:dev"
    assert prod.build is not None and prod.build.commands == (("build-prod",),)
    assert [item.destination for item in prod.artifacts] == ["dist"]
    assert prod.build_origin == "project"


def test_remote_docker_override_replaces_without_deep_merge(tmp_path: Path) -> None:
    """A remote Docker table is complete and does not inherit project image/network."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "same.example"
[remotes.prod]
protocol = "sftp"
host = "same.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[projects.demo.build]
runner = "docker"
commands = [["prod-build"]]
[projects.demo.build.docker]
image = "prod@sha256:abc"
network = "bridge"
[projects.demo.remotes.dev.build]
runner = "docker"
commands = [["dev-build"]]
[projects.demo.remotes.dev.build.docker]
image = "dev@sha256:def"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    dev = select_remote(config, "dev")[2]["demo"].build
    prod = select_remote(config, "prod")[2]["demo"].build
    assert dev is not None and dev.docker is not None
    assert dev.docker.image == "dev@sha256:def"
    assert dev.docker.network == "none"
    assert prod is not None and prod.docker is not None
    assert prod.docker.image == "prod@sha256:abc"
    assert prod.docker.network == "bridge"


def test_remote_onepassword_override_does_not_inherit_prod_reference(tmp_path: Path) -> None:
    """Remote secret mappings replace project mappings so dev never inherits prod."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[remotes.dev]
protocol = "sftp"
host = "same.example"
[remotes.prod]
protocol = "sftp"
host = "same.example"
[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
[projects.demo.build]
commands = [["prod-build"]]
env_allowlist = ["TOKEN"]
[projects.demo.build.onepassword.env]
TOKEN = "op://prod/item/token"
[projects.demo.remotes.dev.build]
commands = [["dev-build"]]
env_allowlist = ["TOKEN"]
[projects.demo.remotes.dev.build.onepassword.env]
TOKEN = "op://dev/item/token"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    dev = select_remote(config, "dev")[2]["demo"].build
    prod = select_remote(config, "prod")[2]["demo"].build
    assert dev is not None and dev.onepassword is not None
    assert prod is not None and prod.onepassword is not None
    assert dev.onepassword.as_dict()["TOKEN"] == "op://dev/item/token"
    assert prod.onepassword.as_dict()["TOKEN"] == "op://prod/item/token"


@pytest.mark.parametrize(
    "value",
    ["relative", "/srv/../escape", "/srv/./dot", "/srv\\windows", "/srv/control\x01"],
)
def test_remote_root_rejects_non_posix_traversal_and_control(value: str) -> None:
    """Reject remote roots that cannot be safely joined before any connection."""

    from git_deploy.config import _validate_remote_root

    with pytest.raises(ConfigurationError):
        _validate_remote_root(value, "projects.demo.remote_root")


def test_remote_root_normalizes_duplicate_slashes_and_allows_unicode() -> None:
    """Canonicalize harmless separators while retaining valid Unicode segments."""

    from git_deploy.config import _validate_remote_root

    assert _validate_remote_root("/srv//应用///", "root") == "/srv/应用"


def test_ftps_tls_paths_resolve_relative_to_toml_directory(tmp_path: Path) -> None:
    """P1-08: tls_ca_file/tls_cert_file/tls_key_file follow the same
    config-relative rule as every other path in deploy.toml."""

    repository = tmp_path / "repository"
    repository.mkdir()
    certs = tmp_path / "certs"
    certs.mkdir()
    (certs / "ca.pem").write_bytes(b"placeholder-ca")
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "ftps"
host = "ftp.example.com"
tls_ca_file = "certs/ca.pem"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    _remote_name, server, _projects = select_remote(config, None)
    assert server.values["tls_ca_file"] == str((tmp_path / "certs" / "ca.pem").resolve())


def test_ftps_tls_ca_file_missing_is_rejected_at_config_load(tmp_path: Path) -> None:
    """P1-08: a missing FTPS CA file must fail as a structured config error,
    before any connection is attempted."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "ftps"
host = "ftp.example.com"
tls_ca_file = "certs/missing-ca.pem"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="tls_ca_file does not exist"):
        load_config(config_path)


def test_ftps_legacy_cert_file_alias_also_resolves_and_validates(tmp_path: Path) -> None:
    """P1-08 follow-up: the undocumented cert_file/ca_file/key_file aliases
    accepted by build_ftps_ssl_context must get the same config-relative
    resolution and load-time existence check as tls_ca_file, not silently
    stay unresolved against the process CWD."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "ftps"
host = "ftp.example.com"
ca_file = "certs/missing-ca.pem"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="ca_file does not exist"):
        load_config(config_path)


def test_ftps_tls_paths_ignored_for_non_ftps_protocol(tmp_path: Path) -> None:
    """A stray tls_ca_file on a non-FTPS remote must not block config load."""

    repository = tmp_path / "repository"
    repository.mkdir()
    config_path = tmp_path / "deploy.toml"
    config_path.write_text(
        f"""
[server]
protocol = "sftp"
host = "sftp.example.com"
tls_ca_file = "certs/missing-ca.pem"

[projects.demo]
repository = "{repository}"
remote_root = "/srv/demo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)
    _remote_name, server, _projects = select_remote(config, None)
    # Still normalized to an absolute path even though existence is not enforced.
    assert server.values["tls_ca_file"] == str(
        (tmp_path / "certs" / "missing-ca.pem").resolve()
    )
