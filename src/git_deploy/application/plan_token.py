"""Signed operation plan tokens that reject stale execution context."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any

from ..errors import StalePlanError
from .models import OperationRequest

__all__ = ["OperationPlanToken", "PlanTokenSigner", "StalePlanError", "confirmation_policy_fingerprint"]


@dataclass(frozen=True, slots=True)
class OperationPlanToken:
    """Opaque signed credential authorizing one exact operation plan."""

    value: str

    def __post_init__(self) -> None:
        """Validate the opaque token representation."""

        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("plan token must be a non-empty string")

    def __str__(self) -> str:
        """Return the opaque representation for adapter serialization."""

        return self.value


class PlanTokenSigner:
    """Issue and verify HMAC-bound operation plan credentials."""

    def __init__(self, key: bytes):
        """Create a signer with a caller-owned process or state secret.

        Args:
            key: At least 32 bytes of unpredictable signing material.
        """

        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("plan token signing key must contain at least 32 bytes")
        self._key = key

    def issue(
        self,
        request: OperationRequest,
        *,
        policy_fingerprint: str,
        plan_digest: str,
    ) -> OperationPlanToken:
        """Sign one request and all stale-plan execution boundaries.

        Args:
            request: Full immutable request, including target and generation.
            policy_fingerprint: Managed/confirmation policy fingerprint.
            plan_digest: Digest of the exact structured operation plan.

        Returns:
            Opaque signed operation plan token.
        """

        payload = _token_payload(request, policy_fingerprint, plan_digest)
        encoded = _b64encode(payload)
        signature = _b64encode(hmac.digest(self._key, encoded, "sha256"))
        return OperationPlanToken(f"v1.{encoded.decode()}.{signature.decode()}")

    def verify(
        self,
        token: OperationPlanToken,
        request: OperationRequest,
        *,
        policy_fingerprint: str,
        plan_digest: str,
    ) -> None:
        """Reject a forged token or any changed execution boundary.

        Args:
            token: Previously issued opaque credential.
            request: Request presented for execution.
            policy_fingerprint: Current policy fingerprint.
            plan_digest: Current structured plan digest.

        Raises:
            StalePlanError: If format, signature, request, policy, or plan differs.
        """

        if not isinstance(token, OperationPlanToken):
            raise TypeError("token must be an OperationPlanToken")
        expected = self.issue(
            request,
            policy_fingerprint=policy_fingerprint,
            plan_digest=plan_digest,
        )
        if not hmac.compare_digest(token.value, expected.value):
            raise StalePlanError(
                "operation plan is stale or does not match the execution context"
            )


def confirmation_policy_fingerprint(policy: object) -> str:
    """Return a canonical digest for an immutable confirmation policy.

    Args:
        policy: Dataclass-based confirmation policy.

    Returns:
        Lowercase SHA-256 digest of the canonical policy payload.
    """

    if not is_dataclass(policy) or isinstance(policy, type):
        raise TypeError("policy must be a dataclass instance")
    encoded = json.dumps(
        _canonical_value(policy),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _token_payload(
    request: OperationRequest,
    policy_fingerprint: str,
    plan_digest: str,
) -> bytes:
    """Serialize the exact stale-plan boundaries to canonical JSON bytes."""

    for name, value in (
        ("policy_fingerprint", policy_fingerprint),
        ("plan_digest", plan_digest),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    payload = {
        "version": 1,
        "request_type": request.__class__.__name__,
        "operation": request.operation.value,
        "request": _canonical_value(request),
        "policy_fingerprint": policy_fingerprint,
        "plan_digest": plan_digest,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def _canonical_value(value: Any) -> object:
    """Convert supported immutable contract values to canonical JSON data."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise TypeError(f"unsupported plan token value: {value.__class__.__name__}")


def _b64encode(value: bytes) -> bytes:
    """Return unpadded URL-safe base64 bytes."""

    return base64.urlsafe_b64encode(value).rstrip(b"=")
