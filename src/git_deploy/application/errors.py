"""Stable application errors with recursively redacted context."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    """Stable high-level error categories for CLI/TUI rendering."""

    CONFIGURATION = "configuration"
    POLICY = "policy"
    REMOTE = "remote"
    EXECUTION = "execution"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ErrorContextItem:
    """One sanitized, immutable application error context entry."""

    key: str
    value: object


class ApplicationError(Exception):
    """Renderer-neutral application error with stable code/category/context."""

    __slots__ = ("category", "code", "context", "message")

    def __init__(
        self,
        *,
        code: str,
        category: ErrorCategory,
        message: str,
        context: Mapping[str, Any] | None = None,
    ):
        """Create and sanitize an application error.

        Args:
            code: Stable machine-readable dotted code.
            category: Stable renderer-facing category.
            message: Human-readable message; known secret patterns are masked.
            context: Arbitrary diagnostic context sanitized before retention.
        """

        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            raise ValueError("error code must be a stable lowercase dotted identifier")
        if not isinstance(category, ErrorCategory):
            raise TypeError("category must be an ErrorCategory")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("error message must be a non-empty string")
        safe_message = _redact_text(message)
        safe_context = tuple(
            ErrorContextItem(str(key), _sanitize_context_value(str(key), value))
            for key, value in sorted((context or {}).items(), key=lambda item: str(item[0]))
        )
        super().__init__(safe_message)
        self.code = code
        self.category = category
        self.message = safe_message
        self.context = safe_context

    def to_dict(self) -> dict[str, object]:
        """Return a serialization-ready copy of the sanitized error.

        Returns:
            Stable code/category/message/context mapping.
        """

        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "context": {item.key: item.value for item in self.context},
        }


def application_error_from_exception(
    error: BaseException,
    *,
    context: Mapping[str, Any] | None = None,
) -> ApplicationError:
    """Map existing domain exceptions to stable application errors.

    Args:
        error: Domain or unexpected exception.
        context: Additional diagnostic context to sanitize.

    Returns:
        Existing application error or a stable wrapped error.
    """

    if isinstance(error, ApplicationError):
        return error

    from git_deploy.errors import (
        ConfigurationError,
        GitDeployError,
        PolicyError,
        RemoteDriftError,
    )

    if isinstance(error, ConfigurationError):
        code = "configuration.invalid"
        category = ErrorCategory.CONFIGURATION
    elif isinstance(error, PolicyError):
        code = "policy.blocked"
        category = ErrorCategory.POLICY
    elif isinstance(error, RemoteDriftError):
        code = "remote.drift"
        category = ErrorCategory.REMOTE
    elif isinstance(error, KeyboardInterrupt):
        code = "operation.cancelled"
        category = ErrorCategory.CANCELLED
    elif isinstance(error, GitDeployError):
        code = "operation.failed"
        category = ErrorCategory.EXECUTION
    else:
        code = "internal.unexpected"
        category = ErrorCategory.INTERNAL
    return ApplicationError(
        code=code,
        category=category,
        message=str(error) or error.__class__.__name__,
        context=context,
    )


_ERROR_CODE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_REDACTED = "***"
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "passphrase",
        "passwd",
        "password",
        "private",
        "reference",
        "secret",
        "session",
        "token",
        "value",
    }
)
_OP_REFERENCE = re.compile(r"op://[^\s\"']+")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,\"']+")
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(token|password|passwd|secret|authorization)=([^\s,;]+)"
)
_KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{8,})\b")


def _sanitize_context_value(key: str, value: Any) -> object:
    """Recursively sanitize one context value without retaining object reprs.

    Args:
        key: Context key used for sensitive-name classification.
        value: Candidate diagnostic value.

    Returns:
        Immutable sanitized scalar or tuple.
    """

    if _sensitive_key(key):
        return _REDACTED
    if value is None or type(value) in {int, float, bool}:
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return tuple(
            (str(child_key), _sanitize_context_value(str(child_key), child_value))
            for child_key, child_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_sanitize_context_value(key, item) for item in value)
    return f"<{value.__class__.__name__}>"


def _sensitive_key(key: str) -> bool:
    """Return whether a context key denotes secret-bearing data."""

    normalized = key.strip().lower()
    if normalized.startswith("op_"):
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str) -> str:
    """Mask known secret/reference patterns in diagnostic text."""

    masked = _OP_REFERENCE.sub(_REDACTED, value)
    masked = _BEARER.sub(f"Bearer {_REDACTED}", masked)
    masked = _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", masked)
    return _KNOWN_TOKEN.sub(_REDACTED, masked)
