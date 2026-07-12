"""Read-only remote verification unit tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from git_deploy.expected_state import FileEntry, build_expected_state
from git_deploy.models import ProjectConfig
from git_deploy.remote_verify import classify_remote_path, verify_remote_current
from git_deploy.state_executor import FakeRemotePath, InMemoryTransport


def test_classify_match_absent_drift() -> None:
    """Classifier covers match, absent, and drift for current snapshot semantics."""

    entry = FileEntry(path="a.txt", owner="source", content_sha256=hashlib.sha256(b"ok").hexdigest())
    match = classify_remote_path(entry, b"ok", remote_path="/srv/a.txt")
    assert match.status == "match"
    absent = classify_remote_path(entry, None, remote_path="/srv/a.txt")
    assert absent.status == "absent"
    drift = classify_remote_path(entry, b"nope", remote_path="/srv/a.txt")
    assert drift.status == "drift"


def test_verify_remote_current_zero_writes(tmp_path: Path) -> None:
    """verify_remote_current reads paths and never increments write_calls."""

    project = ProjectConfig(name="demo", repository=tmp_path, remote_root="/srv")
    digest = hashlib.sha256(b"body").hexdigest()
    state = build_expected_state(
        generation=1,
        parent_state_id=None,
        source_tree_id="t",
        applied_transition_ids=(),
        physical_fingerprint="p",
        policy_fingerprint="q",
        files=(FileEntry(path="a.txt", owner="source", content_sha256=digest),),
    )
    transport = InMemoryTransport()
    transport.files["/srv/a.txt"] = FakeRemotePath(b"body")
    report = verify_remote_current(state, project, transport)
    assert report.ok
    assert report.write_calls == 0
    assert transport.write_calls == 0
    assert transport.read_calls >= 1
