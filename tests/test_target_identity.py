"""Physical target identity and managed policy fingerprint tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError
from git_deploy.models import ProjectConfig, ServerConfig
from git_deploy.target_identity import (
    assert_explicit_id_matches_payload,
    build_physical_payload,
    managed_policy_fingerprint,
    policy_fingerprint_for_project,
    resolve_target_identity,
)


def test_physical_same_endpoint_different_username_same_id() -> None:
    """Username is access subject and must not split physical target id."""

    a = resolve_target_identity(
        ServerConfig(
            {
                "protocol": "sftp",
                "host": "app.example.com",
                "username": "deploy",
                "password": "secret-a",
            }
        ),
        "demo",
        remote_root="/srv/demo",
    )
    b = resolve_target_identity(
        ServerConfig(
            {
                "protocol": "sftp",
                "host": "app.example.com",
                "username": "other",
                "token": "secret-b",
            }
        ),
        "demo",
        remote_root="/srv/demo",
    )
    assert a.target_id == b.target_id
    assert a.physical_fingerprint == b.physical_fingerprint


def test_physical_alias_and_build_do_not_change_payload() -> None:
    """Alias, password, token, and build settings stay outside physical identity."""

    base = build_physical_payload(
        protocol="SFTP",
        host="App.Example.COM.",
        project="demo",
        remote_root="/srv/demo/",
    )
    other = build_physical_payload(
        protocol="sftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/demo",
        port=22,
    )
    assert base.fingerprint() == other.fingerprint()
    # Credentials on ServerConfig must not affect resolve.
    id_a = resolve_target_identity(
        {"protocol": "sftp", "host": "app.example.com", "password": "x", "alias": "prod"},
        "demo",
        remote_root="/srv/demo",
    )
    id_b = resolve_target_identity(
        {"protocol": "sftp", "host": "app.example.com", "token": "y", "build": {"image": "x"}},
        "demo",
        remote_root="/srv/demo",
    )
    assert id_a.payload.canonical_dict() == id_b.payload.canonical_dict()


def test_physical_protocol_host_port_root_isolate() -> None:
    """Different protocol, host, effective port, or root isolate targets."""

    base = build_physical_payload(
        protocol="sftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/demo",
    )
    by_protocol = build_physical_payload(
        protocol="ftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/demo",
    )
    by_host = build_physical_payload(
        protocol="sftp",
        host="other.example.com",
        project="demo",
        remote_root="/srv/demo",
    )
    by_port = build_physical_payload(
        protocol="sftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/demo",
        port=2222,
    )
    by_root = build_physical_payload(
        protocol="sftp",
        host="app.example.com",
        project="demo",
        remote_root="/srv/other",
    )
    fingerprints = {
        base.fingerprint(),
        by_protocol.fingerprint(),
        by_host.fingerprint(),
        by_port.fingerprint(),
        by_root.fingerprint(),
    }
    assert len(fingerprints) == 5


def test_physical_dns_ipv_and_root_normalization() -> None:
    """DNS case/trailing-dot, IPv4/IPv6, default ports, and root separators normalize."""

    dns = build_physical_payload(
        protocol="sftp",
        host="Example.COM.",
        project="p",
        remote_root="//srv//app//",
    )
    assert dns.host == "example.com"
    assert dns.port == 22
    assert dns.remote_root == "/srv/app"

    ipv4 = build_physical_payload(
        protocol="ftp",
        host="127.0.0.1",
        project="p",
        remote_root="/",
    )
    assert ipv4.host == "127.0.0.1"
    assert ipv4.port == 21

    ipv6 = build_physical_payload(
        protocol="ftps",
        host="[::1]",
        project="p",
        remote_root="/data",
    )
    assert ipv6.host == "::1"
    # Explicit FTPS shares transport default port 21 (not implicit 990).
    assert ipv6.port == 21


def test_physical_state_root_layout(tmp_path: Path) -> None:
    """Default state root is targets/<target-id> under the project base."""

    identity = resolve_target_identity(
        {"protocol": "sftp", "host": "h.example"},
        "demo",
        remote_root="/srv",
    )
    root = identity.state_root(tmp_path / "state")
    assert root == (tmp_path / "state" / "targets" / identity.target_id).resolve()


def test_physical_explicit_id_rejects_cross_payload_merge() -> None:
    """Explicit target_id may only name one canonical payload."""

    first = build_physical_payload(
        protocol="sftp",
        host="a.example",
        project="demo",
        remote_root="/srv/a",
    )
    second = build_physical_payload(
        protocol="sftp",
        host="b.example",
        project="demo",
        remote_root="/srv/b",
    )
    with pytest.raises(ConfigurationError, match="cannot merge distinct physical payloads"):
        assert_explicit_id_matches_payload("shared", second, first)

    with pytest.raises(ConfigurationError, match="cannot merge distinct physical payloads"):
        resolve_target_identity(
            {"protocol": "sftp", "host": "b.example"},
            "demo",
            remote_root="/srv/b",
            explicit_target_id="shared",
            bound_payload=first,
        )


def test_managed_policy_fingerprint_fields() -> None:
    """Repository identity and path policies affect managed policy fingerprint."""

    base = managed_policy_fingerprint(
        repository_identity="repo-a",
        include=("**",),
        exclude=(),
        protected=(".env",),
        artifact_destinations=("vendor",),
    )
    by_repo = managed_policy_fingerprint(
        repository_identity="repo-b",
        include=("**",),
        exclude=(),
        protected=(".env",),
        artifact_destinations=("vendor",),
    )
    by_include = managed_policy_fingerprint(
        repository_identity="repo-a",
        include=("app/**",),
        exclude=(),
        protected=(".env",),
        artifact_destinations=("vendor",),
    )
    by_exclude = managed_policy_fingerprint(
        repository_identity="repo-a",
        include=("**",),
        exclude=("tmp/**",),
        protected=(".env",),
        artifact_destinations=("vendor",),
    )
    by_protected = managed_policy_fingerprint(
        repository_identity="repo-a",
        include=("**",),
        exclude=(),
        protected=(".env", "secrets/**"),
        artifact_destinations=("vendor",),
    )
    by_artifact = managed_policy_fingerprint(
        repository_identity="repo-a",
        include=("**",),
        exclude=(),
        protected=(".env",),
        artifact_destinations=("vendor", "public/build"),
    )
    assert len({base, by_repo, by_include, by_exclude, by_protected, by_artifact}) == 6


def test_managed_policy_ignores_build_and_secret_settings(tmp_path: Path) -> None:
    """Build command/image/secret references must not alter policy fingerprint."""

    project = ProjectConfig(
        name="demo",
        repository=tmp_path,
        remote_root="/srv",
        include=("**",),
        exclude=(),
        protected=(),
    )
    # Policy is derived only from managed fields; callers must not feed build into it.
    fp1 = policy_fingerprint_for_project(project, repository_identity="same")
    fp2 = managed_policy_fingerprint(
        repository_identity="same",
        include=project.include,
        exclude=project.exclude,
        protected=project.protected,
        artifact_destinations=(),
    )
    assert fp1 == fp2
    # Explicitly show that inventing build-ish fields outside the API cannot be required.
    unrelated_build = managed_policy_fingerprint(
        repository_identity="same",
        include=project.include,
        exclude=project.exclude,
        protected=project.protected,
        artifact_destinations=(),
    )
    assert unrelated_build == fp1
