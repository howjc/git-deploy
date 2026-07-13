"""Validated SFTP ownership and permission policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ConfigurationError


SFTP_PERMISSION_KEYS = frozenset(
    {"owner", "group", "file_mode", "executable_mode", "directory_mode"}
)


@dataclass(frozen=True, slots=True)
class SftpPermissionPolicy:
    """Permissions applied to files and directories created through SFTP."""

    owner: str | None = None
    group: str | None = None
    file_mode: int = 0o644
    executable_mode: int = 0o755
    directory_mode: int = 0o755


def load_sftp_permission_policy(
    server: Mapping[str, Any],
    *,
    location: str = "server",
) -> SftpPermissionPolicy:
    """Parse and validate one remote's SFTP permission settings.

    Args:
        server: Raw server or named-remote mapping.
        location: Configuration path used in validation errors.

    Returns:
        Immutable SFTP permission policy with safe defaults.

    Raises:
        ConfigurationError: If modes, identities, or protocol usage are invalid.
    """

    protocol = str(server.get("protocol", "sftp")).strip().lower()
    configured = SFTP_PERMISSION_KEYS.intersection(server)
    if protocol != "sftp" and configured:
        fields = ", ".join(sorted(configured))
        raise ConfigurationError(
            f"{location} fields {fields} require protocol='sftp'; "
            f"{protocol.upper()} cannot guarantee POSIX ownership or modes"
        )
    return SftpPermissionPolicy(
        owner=_identity(server.get("owner"), "owner", location),
        group=_identity(server.get("group"), "group", location),
        file_mode=_mode(server.get("file_mode"), "file_mode", 0o644, location),
        executable_mode=_mode(
            server.get("executable_mode"),
            "executable_mode",
            0o755,
            location,
        ),
        directory_mode=_mode(
            server.get("directory_mode"),
            "directory_mode",
            0o755,
            location,
        ),
    )


_IDENTITY = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*\$?|[0-9]+)$")
_OCTAL_MODE = re.compile(r"^[0-7]{3,4}$")


def _identity(value: object, field: str, location: str) -> str | None:
    """Return a safe user/group name or numeric ID from configuration."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigurationError(f"{location}.{field} must be a user/group name or numeric ID")
    if isinstance(value, int):
        if value < 0:
            raise ConfigurationError(
                f"{location}.{field} must be a non-negative user/group ID"
            )
        return str(value)
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value.strip()):
        raise ConfigurationError(
            f"{location}.{field} must be a safe user/group name or numeric ID"
        )
    return value.strip()


def _mode(value: object, field: str, default: int, location: str) -> int:
    """Return a basic POSIX mode without setuid/setgid/sticky bits."""

    if value is None:
        return default
    if isinstance(value, bool):
        parsed = -1
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _OCTAL_MODE.fullmatch(value.strip()):
        parsed = int(value.strip(), 8)
    else:
        parsed = -1
    if not 0 <= parsed <= 0o777:
        raise ConfigurationError(
            f"{location}.{field} must be an octal string such as '0644' "
            "or a TOML octal integer such as 0o644"
        )
    return parsed
