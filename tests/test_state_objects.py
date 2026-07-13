"""Content-addressed object store tests (S05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.errors import ConfigurationError
from git_deploy.object_store import ContentAddressedStore


def test_cas_put_get_dedup_permissions_and_tamper(tmp_path: Path) -> None:
    """CAS durable write, dedup, permission, rehash, and tamper rejection."""

    store = ContentAddressedStore(tmp_path / "target")
    digest = store.put(b"hello")
    assert store.put(b"hello") == digest
    assert store.get(digest) == b"hello"
    assert store.permission_mode(digest) == 0o600
    assert store.contains(digest)

    path = store.path_for(digest)
    path.write_bytes(b"tampered")
    with pytest.raises(ConfigurationError, match="digest mismatch"):
        store.get(digest)


def test_cas_readable_after_reopen(tmp_path: Path) -> None:
    """Objects written by one store instance remain readable from another."""

    root = tmp_path / "target"
    first = ContentAddressedStore(root)
    digest = first.put(b"reopen-me")
    second = ContentAddressedStore(root)
    assert second.get(digest) == b"reopen-me"
