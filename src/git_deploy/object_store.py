"""SHA-256 content-addressed object store using durable atomic publish."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from .durable_io import durable_publish, durable_publish_stream, ensure_state_directory, file_mode
from .errors import ConfigurationError


class ContentAddressedStore:
    """Persist opaque bytes under ``objects/sha256/<prefix>/<digest>``."""

    def __init__(self, target_root: Path):
        """Bind the CAS root under a target state directory.

        Args:
            target_root: ``.../targets/<target-id>`` directory.
        """

        self.root = target_root.resolve()
        self.objects_dir = self.root / "objects" / "sha256"

    def ensure_layout(self) -> None:
        """Create the CAS directory tree with owner-only permissions.

        Returns:
            None.
        """

        ensure_state_directory(self.root)
        ensure_state_directory(self.objects_dir)

    def put(self, data: bytes) -> str:
        """Store bytes if absent and return the content digest.

        Args:
            data: Exact bytes to store.

        Returns:
            Lowercase SHA-256 hex digest.
        """

        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest)
        if path.is_file():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ConfigurationError(f"CAS object corrupted at {path}")
            return digest
        self.ensure_layout()
        ensure_state_directory(path.parent)
        durable_publish(path, data)
        return digest

    def get(self, digest: str) -> bytes:
        """Read bytes and recompute the digest for integrity.

        Args:
            digest: Expected SHA-256 hex digest.

        Returns:
            Stored bytes.

        Raises:
            ConfigurationError: When missing or tampered.
        """

        path = self.path_for(digest)
        if not path.is_file():
            raise ConfigurationError(f"CAS object not found: {digest}")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest.lower():
            raise ConfigurationError(
                f"CAS object digest mismatch for {digest}: actual={actual}"
            )
        return data

    def put_stream(self, chunks: Iterable[bytes], *, expected_digest: str) -> str:
        """Store a chunk iterable and require its final digest to match.

        Args:
            chunks: Iterable of bytes chunks.
            expected_digest: Planned SHA-256 digest.

        Returns:
            Verified lowercase digest.
        """

        path = self.path_for(expected_digest)
        if path.is_file():
            self.get(expected_digest)
            return expected_digest.lower()
        self.ensure_layout()
        ensure_state_directory(path.parent)
        actual, _size = durable_publish_stream(
            path, chunks, expected_digest=expected_digest
        )
        return actual

    def contains(self, digest: str) -> bool:
        """Return whether a digest is present without reading full integrity.

        Args:
            digest: SHA-256 hex digest.

        Returns:
            ``True`` when the object path exists.
        """

        return self.path_for(digest).is_file()

    def path_for(self, digest: str) -> Path:
        """Return the on-disk path for one digest.

        Args:
            digest: SHA-256 hex digest.

        Returns:
            Absolute object path.
        """

        value = digest.lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ConfigurationError(f"invalid CAS digest: {digest!r}")
        return self.objects_dir / value[:2] / value

    def permission_mode(self, digest: str) -> int:
        """Return the permission bits of a stored object.

        Args:
            digest: SHA-256 hex digest.

        Returns:
            Mode bits masked to ``0o777``.
        """

        return file_mode(self.path_for(digest))
