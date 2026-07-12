"""Transaction journal state machine tests (S10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_deploy.durable_io import set_fault_hook
from git_deploy.errors import ConfigurationError
from git_deploy.transaction import VALID_STAGES, TransactionStore


@pytest.fixture(autouse=True)
def _reset_fault() -> None:
    """Clear fault hooks.

    Returns:
        None.
    """

    yield
    set_fault_hook(None)


def test_state_machine_rejects_illegal_and_persists_stages(tmp_path: Path) -> None:
    """Illegal transitions fail; all north-star stages durable-publish and reopen."""

    store = TransactionStore(tmp_path / "target")
    journal = store.create(
        target_id="tgt",
        stage="prepared",
        before_state_id="before",
        after_state_id="after",
        before_generation=1,
        after_generation=2,
        backup_refs=["backups/00000.bin"],
    )
    assert journal.stage == "prepared"
    with pytest.raises(ConfigurationError, match="illegal transaction transition"):
        store.advance(journal, "state_committed")

    journal = store.advance(journal, "remote_mutating")
    journal = store.advance(journal, "remote_verified")
    journal = store.advance(journal, "state_committed")
    journal = store.advance(journal, "recovered")
    reopened = TransactionStore(tmp_path / "target").load(journal.transaction_id)
    assert reopened.stage == "recovered"
    assert reopened.backup_refs == ["backups/00000.bin"]

    # Cover remaining stages via independent journals.
    for stage in sorted(VALID_STAGES):
        if stage == "recovered":
            continue
        if stage in {"prepared", "reconciled"}:
            j = store.create(target_id="tgt", stage=stage)
        else:
            j = store.create(target_id="tgt", stage="prepared")
            if stage == "remote_mutating":
                j = store.advance(j, "remote_mutating")
            elif stage == "remote_verified":
                j = store.advance(j, "remote_mutating")
                j = store.advance(j, "remote_verified")
            elif stage == "state_committed":
                j = store.advance(j, "remote_mutating")
                j = store.advance(j, "remote_verified")
                j = store.advance(j, "state_committed")
            elif stage == "manual_recovery_required":
                j = store.advance(j, "manual_recovery_required")
            elif stage == "recovered":
                pass
        loaded = store.load(j.transaction_id)
        assert loaded.stage in VALID_STAGES


def test_state_machine_fault_does_not_advance_stage(tmp_path: Path) -> None:
    """Write/fsync fault while advancing must not leave an illegal half stage as current."""

    store = TransactionStore(tmp_path / "target")
    journal = store.create(target_id="tgt", stage="prepared")

    def fault(stage: str, path: Path) -> None:
        if stage == "after_write" and path.name.endswith(".json"):
            raise RuntimeError("fsync-kill")

    set_fault_hook(fault)
    with pytest.raises(RuntimeError, match="fsync-kill"):
        store.advance(journal, "remote_mutating")
    set_fault_hook(None)
    reopened = store.load(journal.transaction_id)
    assert reopened.stage == "prepared"


def test_unknown_schema_and_stage_rejected(tmp_path: Path) -> None:
    """Tampered schema/stage journals are rejected on load."""

    store = TransactionStore(tmp_path / "target")
    journal = store.create(target_id="tgt", stage="prepared")
    path = store.path_for(journal.transaction_id)
    path.write_text('{"schema_version": 99, "stage": "prepared", "transaction_id": "x", "target_id": "t"}')
    with pytest.raises(ConfigurationError, match="unknown transaction journal schema"):
        store.load(journal.transaction_id)
