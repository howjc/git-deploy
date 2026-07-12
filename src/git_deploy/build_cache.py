"""Runner-neutral build fingerprints and CAS-backed artifact cache."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import ArtifactManifest
from .durable_io import durable_publish, ensure_state_directory
from .errors import ConfigurationError
from .models import ArtifactConfig, BuildConfig
from .object_store import ContentAddressedStore


@dataclass(frozen=True)
class CachedArtifact:
    """One artifact record backed by the target content-addressed store."""

    owner: str
    destination: str
    content_sha256: str
    size: int
    executable: bool


@dataclass(frozen=True)
class BuildCacheEntry:
    """Validated immutable cache entry."""

    fingerprint: str
    source_tree_id: str
    artifacts: tuple[CachedArtifact, ...]


@dataclass(frozen=True)
class BuildCacheLookup:
    """Cache lookup outcome with an explicit bypass/miss reason."""

    hit: bool
    reason: str
    entry: BuildCacheEntry | None = None


def build_fingerprint(
    *,
    source_tree_id: str,
    build: BuildConfig,
    artifacts: Sequence[ArtifactConfig],
    tool_versions: Mapping[str, str] | None = None,
    lock_digests: Mapping[str, str] | None = None,
    runner_identity: Mapping[str, Any] | None = None,
) -> str:
    """Hash every reproducibility input without including secret URI/value data.

    Args:
        source_tree_id: Exact worktree source tree.
        build: Resolved build configuration.
        artifacts: Resolved artifact mappings.
        tool_versions: Tool name-to-version identity.
        lock_digests: Lockfile path-to-content digest.
        runner_identity: Backend-specific immutable identity fields.

    Returns:
        Stable lowercase SHA-256 fingerprint.
    """

    payload: dict[str, Any] = {
        "schema": 1,
        "source_tree_id": source_tree_id,
        "runner": build.runner,
        "commands": [list(command) for command in build.commands],
        "cwd": build.cwd,
        "timeout": build.timeout,
        "env_names": list(build.env_allowlist),
        "artifacts": [
            {
                "source": item.source,
                "destination": item.destination,
                "kind": item.kind,
            }
            for item in artifacts
        ],
        "tool_versions": dict(sorted((tool_versions or {}).items())),
        "lock_digests": dict(sorted((lock_digests or {}).items())),
        "runner_identity": dict(sorted((runner_identity or {}).items())),
        "secret_provider": (
            {
                "provider": "1password",
                "env_names": [name for name, _reference in build.onepassword.env],
            }
            if build.onepassword is not None
            else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def docker_runner_identity(build: BuildConfig, image_id: str) -> dict[str, Any]:
    """Return every Docker backend field that must invalidate build cache."""

    docker = build.docker
    if build.runner != "docker" or docker is None:
        raise ConfigurationError("docker runner identity requires Docker build config")
    return {
        "backend": "docker",
        "image_id": image_id,
        "platform": docker.platform,
        "network": docker.network,
        "pull_policy": docker.pull_policy,
        "uid": os.getuid(),
        "gid": os.getgid(),
    }


class BuildCache:
    """Persist artifact manifests while keeping bytes solely in the shared CAS."""

    def __init__(self, target_root: Path):
        """Bind a target-scoped build cache and content store."""

        self.target_root = target_root.resolve()
        self.root = self.target_root / "build-cache" / "entries"
        self.cas = ContentAddressedStore(self.target_root)

    def lookup(self, fingerprint: str, *, secrets_enabled: bool = False) -> BuildCacheLookup:
        """Load and integrity-check a cache entry, or explain its miss/bypass."""

        if secrets_enabled:
            return BuildCacheLookup(False, "1password builds always bypass cache")
        path = self.root / f"{fingerprint}.json"
        if not path.is_file():
            return BuildCacheLookup(False, "cache miss")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("fingerprint") != fingerprint:
                raise ConfigurationError("build cache fingerprint mismatch")
            artifacts = tuple(
                CachedArtifact(
                    owner=str(item["owner"]),
                    destination=str(item["destination"]),
                    content_sha256=str(item["content_sha256"]),
                    size=int(item["size"]),
                    executable=bool(item["executable"]),
                )
                for item in payload.get("artifacts", [])
            )
            for item in artifacts:
                data = self.cas.get(item.content_sha256)
                if len(data) != item.size:
                    raise ConfigurationError(
                        f"build cache artifact size mismatch: {item.destination}"
                    )
            entry = BuildCacheEntry(
                fingerprint=fingerprint,
                source_tree_id=str(payload["source_tree_id"]),
                artifacts=artifacts,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, ConfigurationError):
                raise
            raise ConfigurationError(f"invalid build cache entry {path}: {exc}") from exc
        return BuildCacheLookup(True, "cache hit", entry)

    def store(
        self,
        fingerprint: str,
        source_tree_id: str,
        manifest: ArtifactManifest,
        *,
        secrets_enabled: bool = False,
    ) -> BuildCacheEntry | None:
        """Publish artifact bytes then an immutable cache manifest.

        Secret-enabled builds deliberately return ``None`` and persist nothing.
        """

        records: list[CachedArtifact] = []
        for item in manifest.files:
            data = item.source_path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != item.sha256 or len(data) != item.size:
                raise ConfigurationError(
                    f"artifact changed after collection: {item.destination}"
                )
            stored = self.cas.put(data)
            records.append(
                CachedArtifact(
                    owner=item.owner,
                    destination=item.destination,
                    content_sha256=stored,
                    size=item.size,
                    executable=item.executable,
                )
            )
        entry = BuildCacheEntry(fingerprint, source_tree_id, tuple(records))
        if secrets_enabled:
            # Artifact bytes are needed by the imminent transaction, but no
            # reusable manifest is published because secret rotation is opaque.
            return entry
        ensure_state_directory(self.root)
        payload = {
            "schema": 1,
            "fingerprint": fingerprint,
            "source_tree_id": source_tree_id,
            "artifacts": [
                {
                    "owner": item.owner,
                    "destination": item.destination,
                    "content_sha256": item.content_sha256,
                    "size": item.size,
                    "executable": item.executable,
                }
                for item in entry.artifacts
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        durable_publish(self.root / f"{fingerprint}.json", encoded)
        return entry
