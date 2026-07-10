"""Progress event terminal rendering tests."""

from __future__ import annotations

import io

from git_deploy.progress import ProgressEvent, TerminalProgress


def test_non_tty_progress_reports_start_and_completion() -> None:
    """Keep redirected logs informative without printing every byte chunk."""

    stream = io.StringIO()
    renderer = TerminalProgress("demo", stream=stream)

    renderer.update(ProgressEvent("upload", 0, 2, bytes_total=2048))
    renderer.update(
        ProgressEvent(
            "upload",
            0,
            2,
            path="first.bin",
            bytes_completed=1024,
            bytes_total=2048,
        )
    )
    renderer.update(
        ProgressEvent(
            "upload",
            2,
            2,
            path="second.bin",
            bytes_completed=2048,
            bytes_total=2048,
        )
    )
    renderer.finish()
    output = stream.getvalue()

    assert "[demo]" in output
    assert "UPLOAD" in output
    assert "0/2" in output
    assert "2/2" in output
    assert "2.0 KiB/2.0 KiB" in output
