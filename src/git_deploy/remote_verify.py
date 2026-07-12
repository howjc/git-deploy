"""Read-only remote verification against current expected state."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .expected_state import ExpectedState, FileEntry
from .models import ProjectConfig


class ReadableTransport(Protocol):
    """Minimal transport surface for read-only remote verify."""

    def read_file(self, remote_path: str) -> bytes | None:
        """Read remote bytes or ``None`` when absent."""

        ...


@dataclass(frozen=True)
class RemotePathStatus:
    """Classification of one managed remote path vs current expectation.

    Attributes:
        path: Managed relative path.
        remote_path: Absolute remote path.
        status: ``match``, ``absent``, or ``drift``.
        expected_sha256: Expected content hash (or ``None`` when expected absent).
        actual_sha256: Observed content hash (or ``None`` when absent).
    """

    path: str
    remote_path: str
    status: str
    expected_sha256: str | None
    actual_sha256: str | None


@dataclass(frozen=True)
class RemoteVerifyReport:
    """Aggregate read-only remote verification result.

    Attributes:
        results: Per-path classifications.
        write_calls: Transport write counter after verification (must stay 0).
        read_calls: Transport read counter after verification.
    """

    results: tuple[RemotePathStatus, ...]
    write_calls: int
    read_calls: int

    @property
    def ok(self) -> bool:
        """Return whether every path matched the current snapshot.

        Returns:
            ``True`` when all statuses are ``match``.
        """

        return all(item.status == "match" for item in self.results)


def remote_path_for(project: ProjectConfig, relative: str) -> str:
    """Join project remote_root with a managed relative path.

    Args:
        project: Project providing remote_root.
        relative: Managed path relative to remote_root.

    Returns:
        Absolute POSIX remote path.
    """

    root = project.remote_root.rstrip("/") or ""
    rel = relative.lstrip("/")
    return f"{root}/{rel}" if root else f"/{rel}"


def classify_remote_path(
    entry: FileEntry,
    actual: bytes | None,
    *,
    remote_path: str,
) -> RemotePathStatus:
    """Classify actual remote bytes against one current file entry.

    Args:
        entry: Expected managed file entry from current state.
        actual: Bytes read from remote, or ``None`` if missing.
        remote_path: Absolute remote path observed.

    Returns:
        Path status: match / absent / drift.
    """

    actual_hash = hashlib.sha256(actual).hexdigest() if actual is not None else None
    expected_hash = entry.content_sha256 if entry.exists else None
    if not entry.exists:
        status = "match" if actual is None else "drift"
    elif actual is None:
        status = "absent"
    elif actual_hash == expected_hash:
        status = "match"
    else:
        status = "drift"
    return RemotePathStatus(
        path=entry.path,
        remote_path=remote_path,
        status=status,
        expected_sha256=expected_hash,
        actual_sha256=actual_hash,
    )


def verify_remote_current(
    state: ExpectedState,
    project: ProjectConfig,
    transport: ReadableTransport,
) -> RemoteVerifyReport:
    """Read every managed current path and classify match/absent/drift.

    Performs only transport reads; never writes. Callers must assert
    ``write_calls`` is unchanged across the call when the transport tracks it.

    Args:
        state: Current expected snapshot.
        project: Project for remote_root path construction.
        transport: Connected read-capable transport (real or fake).

    Returns:
        Remote verification report with per-path status.
    """

    writes_before = getattr(transport, "write_calls", 0)
    results: list[RemotePathStatus] = []
    for entry in state.files:
        remote_path = remote_path_for(project, entry.path)
        actual = transport.read_file(remote_path)
        results.append(classify_remote_path(entry, actual, remote_path=remote_path))
    writes_after = getattr(transport, "write_calls", 0)
    if writes_after != writes_before:
        raise RuntimeError("remote verify performed transport writes")
    return RemoteVerifyReport(
        results=tuple(results),
        write_calls=writes_after,
        read_calls=getattr(transport, "read_calls", 0),
    )


# Test-only injectable transport factory for CLI remote verify.
_CLI_TRANSPORT_FACTORY: Callable[[dict[str, object]], ReadableTransport] | None = None


def set_cli_transport_factory(
    factory: Callable[[dict[str, object]], ReadableTransport] | None,
) -> None:
    """Install or clear the CLI transport factory (tests only).

    Args:
        factory: Factory opening a transport from server values, or ``None``.

    Returns:
        None.
    """

    global _CLI_TRANSPORT_FACTORY
    _CLI_TRANSPORT_FACTORY = factory


def open_cli_transport(server_values: dict[str, object]) -> ReadableTransport:
    """Open a transport for CLI state verify --check-remote.

    Args:
        server_values: Selected remote server configuration values.

    Returns:
        Connected transport supporting ``read_file``.
    """

    if _CLI_TRANSPORT_FACTORY is not None:
        return _CLI_TRANSPORT_FACTORY(server_values)
    from .transport import open_transport

    transport = open_transport(server_values)
    return transport
