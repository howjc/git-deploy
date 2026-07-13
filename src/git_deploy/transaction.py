"""Durable transaction journal state machine for deploy recovery."""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .durable_io import durable_publish, ensure_state_directory, is_visible_state_file
from .errors import ConfigurationError

JOURNAL_SCHEMA_VERSION = 1

# North-star terminal and intermediate stages.
VALID_STAGES = frozenset(
    {
        "prepared",
        "remote_mutating",
        "remote_verified",
        "state_committed",
        "reconciled",
        "recovered",
        "manual_recovery_required",
    }
)

# Allowed transitions: from_stage -> set of next stages.
# Empty from means initial create.
ALLOWED_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"prepared", "reconciled"}),
    "prepared": frozenset({"remote_mutating", "recovered", "manual_recovery_required", "reconciled"}),
    "remote_mutating": frozenset(
        {"remote_verified", "recovered", "manual_recovery_required"}
    ),
    "remote_verified": frozenset(
        {"state_committed", "manual_recovery_required", "recovered"}
    ),
    "state_committed": frozenset({"recovered"}),  # terminal cleanup only
    "reconciled": frozenset({"recovered"}),
    "recovered": frozenset(),
    "manual_recovery_required": frozenset({"recovered", "state_committed", "remote_verified"}),
}

TERMINAL_STAGES = frozenset({"recovered"})


@dataclass
class TransactionJournal:
    """One durable transaction journal record.

    Attributes:
        transaction_id: Unique journal identifier.
        schema_version: Journal schema version.
        stage: Current north-star stage.
        target_id: Physical target id.
        deployment_id: Related deployment id when applicable.
        before_state_id: State id before mutation.
        after_state_id: State id staged for after.
        before_generation: Generation before CAS.
        after_generation: Generation after CAS.
        backup_refs: Relative backup references prepared before remote writes.
        created_at: ISO-8601 creation timestamp.
        updated_at: ISO-8601 last stage update.
        error: Optional error summary without secrets.
        meta: Opaque recovery metadata.
    """

    transaction_id: str
    schema_version: int
    stage: str
    target_id: str
    deployment_id: str | None = None
    before_state_id: str | None = None
    after_state_id: str | None = None
    before_generation: int | None = None
    after_generation: int | None = None
    backup_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible mapping.

        Returns:
            Serialisable journal payload.
        """

        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TransactionJournal:
        """Parse and validate a journal document.

        Args:
            payload: JSON object.

        Returns:
            Typed journal.
        """

        schema = int(payload.get("schema_version", 0))
        if schema != JOURNAL_SCHEMA_VERSION:
            raise ConfigurationError(f"unknown transaction journal schema: {schema}")
        stage = str(payload.get("stage", ""))
        if stage not in VALID_STAGES:
            raise ConfigurationError(f"unknown transaction stage: {stage!r}")
        return cls(
            transaction_id=str(payload["transaction_id"]),
            schema_version=schema,
            stage=stage,
            target_id=str(payload["target_id"]),
            deployment_id=payload.get("deployment_id"),
            before_state_id=payload.get("before_state_id"),
            after_state_id=payload.get("after_state_id"),
            before_generation=payload.get("before_generation"),
            after_generation=payload.get("after_generation"),
            backup_refs=list(payload.get("backup_refs", [])),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            error=payload.get("error"),
            meta=dict(payload.get("meta", {})),
        )


class TransactionStore:
    """Persist transaction journals with durable publish and stage validation."""

    def __init__(self, target_root: Path):
        """Bind a target transaction directory.

        Args:
            target_root: ``.../targets/<target-id>`` directory.
        """

        self.root = target_root.resolve()
        self.transactions_dir = self.root / "transactions"

    def ensure_layout(self) -> None:
        """Create the transactions directory.

        Returns:
            None.
        """

        ensure_state_directory(self.root)
        ensure_state_directory(self.transactions_dir)

    def path_for(self, transaction_id: str) -> Path:
        """Return the journal path for one transaction id.

        Args:
            transaction_id: Journal identifier.

        Returns:
            Absolute journal path.
        """

        _validate_transaction_id(transaction_id)
        return self.transactions_dir / f"{transaction_id}.json"

    def create(
        self,
        *,
        target_id: str,
        stage: str = "prepared",
        deployment_id: str | None = None,
        before_state_id: str | None = None,
        after_state_id: str | None = None,
        before_generation: int | None = None,
        after_generation: int | None = None,
        backup_refs: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> TransactionJournal:
        """Create and durable-publish a new journal at an initial stage.

        Args:
            target_id: Physical target id.
            stage: Initial stage (must be allowed from ``None``).
            deployment_id: Related deployment id.
            before_state_id: Before state id.
            after_state_id: After state id.
            before_generation: Before generation.
            after_generation: After generation.
            backup_refs: Prepared backup references.
            meta: Opaque metadata.

        Returns:
            Created journal.
        """

        self._assert_transition(None, stage)
        now = _utcnow()
        journal = TransactionJournal(
            transaction_id=_new_transaction_id(),
            schema_version=JOURNAL_SCHEMA_VERSION,
            stage=stage,
            target_id=target_id,
            deployment_id=deployment_id,
            before_state_id=before_state_id,
            after_state_id=after_state_id,
            before_generation=before_generation,
            after_generation=after_generation,
            backup_refs=list(backup_refs or []),
            created_at=now,
            updated_at=now,
            meta=dict(meta or {}),
        )
        self._publish(journal)
        return journal

    def advance(self, journal: TransactionJournal, stage: str, **updates: Any) -> TransactionJournal:
        """Advance a journal to a new stage with durable publish.

        Same-stage updates are allowed only for metadata/field patches (no stage
        change), so callers can attach restore maps without illegal transitions.

        Args:
            journal: Current journal snapshot.
            stage: Next stage (or the current stage for metadata-only updates).
            **updates: Optional field overrides (error, backup_refs, meta, …).

        Returns:
            Updated journal.
        """

        if stage != journal.stage:
            self._assert_transition(journal.stage, stage)
        payload = journal.to_dict()
        payload.update(updates)
        payload["stage"] = stage
        payload["updated_at"] = _utcnow()
        updated = TransactionJournal.from_dict(payload)
        self._publish(updated)
        return updated

    def load(self, transaction_id: str) -> TransactionJournal:
        """Load one journal and re-validate schema/stage.

        Args:
            transaction_id: Journal identifier.

        Returns:
            Parsed journal.
        """

        path = self.path_for(transaction_id)
        if not path.is_file() or not is_visible_state_file(path):
            raise ConfigurationError(f"transaction journal not found: {transaction_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read transaction journal {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("transaction journal root is not an object")
        return TransactionJournal.from_dict(payload)

    def list_open(self) -> list[TransactionJournal]:
        """List non-terminal journals newest-first.

        Returns:
            Open journals.
        """

        journals = [item for item in self.list_all() if item.stage not in TERMINAL_STAGES]
        return journals

    def list_all(self) -> list[TransactionJournal]:
        """List all readable journals newest-first by id.

        Returns:
            Journals sorted by transaction_id descending.
        """

        if not self.transactions_dir.is_dir():
            return []
        results: list[TransactionJournal] = []
        for path in sorted(self.transactions_dir.glob("*.json"), reverse=True):
            if not is_visible_state_file(path):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    results.append(TransactionJournal.from_dict(payload))
            except (OSError, ValueError, TypeError, json.JSONDecodeError, ConfigurationError):
                continue
        return sorted(results, key=lambda item: item.transaction_id, reverse=True)

    def _publish(self, journal: TransactionJournal) -> None:
        """Durable-publish one journal document.

        Args:
            journal: Journal to persist.

        Returns:
            None.
        """

        self.ensure_layout()
        path = self.path_for(journal.transaction_id)
        payload = json.dumps(journal.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        durable_publish(path, payload.encode("utf-8") + b"\n")

    def _assert_transition(self, current: str | None, nxt: str) -> None:
        """Reject illegal stage transitions.

        Args:
            current: Current stage or ``None`` for create.
            nxt: Requested next stage.

        Returns:
            None.
        """

        if nxt not in VALID_STAGES:
            raise ConfigurationError(f"unknown transaction stage: {nxt!r}")
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if nxt not in allowed:
            raise ConfigurationError(
                f"illegal transaction transition: {current!r} -> {nxt!r}"
            )


def _new_transaction_id() -> str:
    """Allocate a unique transaction identifier.

    Returns:
        Timestamp-prefixed random id.
    """

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(6)}"


def _utcnow() -> str:
    """Return an ISO-8601 UTC timestamp.

    Returns:
        Timestamp string with ``Z`` suffix.
    """

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_transaction_id(transaction_id: str) -> None:
    """Reject path-like transaction identifiers.

    Args:
        transaction_id: Candidate id.

    Returns:
        None.
    """

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    if not transaction_id or any(ch not in allowed for ch in transaction_id):
        raise ConfigurationError(f"invalid transaction identifier: {transaction_id!r}")
