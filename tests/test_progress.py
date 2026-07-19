"""Deterministic transfer progress and summary accounting tests."""

from __future__ import annotations

from io import StringIO

import pytest

from git_deploy.progress import MIB, ProgressReporter, format_bytes, format_rate


class FakeClock:
    """Provide manually advanced monotonic timestamps."""

    def __init__(self) -> None:
        """Start the clock at a stable non-zero timestamp."""

        self.now = 100.0

    def __call__(self) -> float:
        """Return the current fake monotonic timestamp."""

        return self.now

    def advance(self, seconds: float) -> None:
        """Advance time by ``seconds`` without sleeping."""

        self.now += seconds


class FakeStream(StringIO):
    """Capture output while exposing configurable TTY behavior."""

    def __init__(self, *, tty: bool) -> None:
        """Create an empty stream with the requested TTY result."""

        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        """Return the configured terminal capability."""

        return self.tty


def test_progress_uses_sliding_rate_and_throttles_tty_updates() -> None:
    """TTY output refreshes at most every 250ms and completes with an average."""

    clock = FakeClock()
    stream = FakeStream(tty=True)
    reporter = ProgressReporter(clock=clock, stream=stream)
    callback = reporter.callback("assets/app.js", 4 * 1024 * 1024)

    callback(0)
    clock.advance(0.1)
    callback(1024 * 1024)
    clock.advance(0.15)
    callback(2 * 1024 * 1024)
    clock.advance(0.75)
    callback(4 * 1024 * 1024)

    output = stream.getvalue()
    assert output.count("UPLOAD assets/app.js") == 3
    assert " 50%" in output
    assert "100%" in output
    assert "avg 4.00 MiB/s" in output
    completed = output.rsplit("\r", 1)[-1]
    assert completed.endswith(" \n")


def test_non_tty_only_renders_completed_file_and_summary() -> None:
    """Redirected output remains line-oriented and omits dynamic updates."""

    clock = FakeClock()
    stream = FakeStream(tty=False)
    reporter = ProgressReporter(clock=clock, stream=stream, label="frontend")
    callback = reporter.callback("dist/app.js", 2 * 1024 * 1024)
    callback(1024)
    clock.advance(2)
    callback(2 * 1024 * 1024)
    reporter.render_summary()

    output = stream.getvalue()
    assert output.count("UPLOAD dist/app.js") == 1
    assert "\r" not in output
    assert "[frontend] TRANSFER SUMMARY" in output
    assert "payload:        2.0 MiB" in output
    assert "average upload: 1.00 MiB/s (8.4 Mbps)" in output


def test_retry_counts_partial_wire_bytes_but_deduplicates_payload() -> None:
    """A failed partial attempt contributes wire/time while payload stays logical."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    first = reporter.callback("app.bin", 1000)
    clock.advance(1)
    first(400)
    reporter.record_retry("app.bin")
    clock.advance(2)
    second = reporter.callback("app.bin", 1000)
    clock.advance(2)
    second(1000)

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 1
    assert summary.payload_bytes == 1000
    assert summary.wire_bytes == 1400
    assert summary.active_seconds == pytest.approx(3)
    assert summary.retries == 1


def test_callback_rollback_starts_an_implicit_retry_without_negative_delta() -> None:
    """A cumulative-byte reset opens a new attempt and preserves prior wire bytes."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    callback = reporter.callback("bundle.js", 100)
    clock.advance(1)
    callback(60)
    clock.advance(1)
    callback(20)
    callback(20)
    clock.advance(1)
    callback(100)

    summary = reporter.finish()
    assert summary is not None
    assert summary.payload_bytes == 100
    assert summary.wire_bytes == 160
    assert summary.active_seconds == pytest.approx(3)
    assert summary.retries == 1


def test_repeated_attempt_registration_counts_retry_without_explicit_hook() -> None:
    """Two physical callbacks for one path are recognized as one retry."""

    reporter = ProgressReporter(stream=FakeStream(tty=False))
    reporter.callback("asset.css", 10)(10)
    reporter.callback("asset.css", 10)(10)

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 1
    assert summary.payload_bytes == 10
    assert summary.wire_bytes == 20
    assert summary.retries == 1


def test_retry_hook_after_completed_upload_retains_logical_success_without_restage() -> None:
    """A later mutation retry does not erase a completed upload when no restage occurs."""

    reporter = ProgressReporter(stream=FakeStream(tty=False))
    reporter.callback("asset.css", 10)(10)
    reporter.record_retry("asset.css")

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 1
    assert summary.payload_bytes == 10
    assert summary.wire_bytes == 10
    assert summary.retries == 1


def test_duplicate_and_oversized_callbacks_do_not_double_count_wire_bytes() -> None:
    """Duplicate cumulative values and values beyond total are clamped safely."""

    reporter = ProgressReporter(stream=FakeStream(tty=False))
    callback = reporter.callback("asset.css", 10)
    callback(5)
    callback(5)
    callback(20)
    callback(20)

    summary = reporter.finish()
    assert summary is not None
    assert summary.wire_bytes == 10
    assert summary.payload_bytes == 10


def test_multiple_files_sum_payload_time_and_average() -> None:
    """Independent files contribute one payload count and additive active time."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    first = reporter.callback("one.bin", 2 * 1024 * 1024)
    clock.advance(1)
    first(2 * 1024 * 1024)
    second = reporter.callback("two.bin", 4 * 1024 * 1024)
    clock.advance(2)
    second(4 * 1024 * 1024)

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 2
    assert summary.payload_bytes == 6 * 1024 * 1024
    assert summary.wire_bytes == 6 * 1024 * 1024
    assert summary.active_seconds == pytest.approx(3)
    assert summary.average_bytes_per_second == pytest.approx(2 * 1024 * 1024)


def test_zero_byte_upload_is_counted_and_no_upload_has_no_summary() -> None:
    """Zero-byte files are logical transfers while an unused reporter stays silent."""

    stream = FakeStream(tty=False)
    unused = ProgressReporter(stream=stream)
    assert unused.finish() is None
    unused.render_summary()
    assert stream.getvalue() == ""

    reporter = ProgressReporter(stream=stream)
    reporter.callback("empty.txt", 0)(0, 0)
    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 1
    assert summary.payload_bytes == 0
    assert summary.wire_bytes == 0
    assert summary.average_bytes_per_second == 0


def test_small_sample_hint_and_formatters_use_iec_units() -> None:
    """Small deployments avoid misleading Mbps precision and format IEC units."""

    stream = FakeStream(tty=False)
    reporter = ProgressReporter(stream=stream)
    reporter.callback("small.txt", 1024)(1024)
    reporter.render_summary()

    assert "(sample too small)" in stream.getvalue()
    assert "Mbps" not in stream.getvalue()
    assert format_bytes(1536) == "1.5 KiB"
    assert format_rate(1536) == "1.50 KiB/s"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (0, "0 B"),
        (17, "17 B"),
        (1536, "1.5 KiB"),
        (3.25 * MIB, "3.2 MiB"),
        (2 * 1024 * MIB, "2.0 GiB"),
    ],
)
def test_byte_formatter_covers_iec_unit_boundaries(value: float, rendered: str) -> None:
    """Byte formatting remains stable from zero through GiB values."""

    assert format_bytes(value) == rendered


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (17, "17 B/s"),
        (1536, "1.50 KiB/s"),
        (3.25 * MIB, "3.20 MiB/s"),
        (2 * 1024 * MIB, "2.00 GiB/s"),
    ],
)
def test_rate_formatter_covers_small_and_iec_rates(value: float, rendered: str) -> None:
    """Rate formatting preserves readable precision at each supported scale."""

    assert format_rate(value) == rendered


def test_sliding_window_evicts_old_samples_and_resets_on_retry() -> None:
    """Current speed ignores stale bytes and a retried attempt starts a fresh window."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=True))
    callback = reporter.callback("window.bin", 1000)
    callback(100)
    clock.advance(1)
    callback(200)
    clock.advance(1)
    callback(300)
    attempt = reporter._attempts["window.bin"]
    assert reporter._window_rate(attempt) == pytest.approx(100)

    reporter.record_retry("window.bin")
    retried = reporter.callback("window.bin", 1000)
    retried(100)
    assert reporter._window_rate(reporter._attempts["window.bin"]) == 0


def test_verbose_mode_still_respects_refresh_throttle() -> None:
    """Verbose mode retains completion lines without per-block output spam."""

    clock = FakeClock()
    stream = FakeStream(tty=True)
    reporter = ProgressReporter(verbose=True, clock=clock, stream=stream)
    callback = reporter.callback("large.bin", 100)
    for value in range(0, 100, 10):
        clock.advance(0.01)
        callback(value)
    callback(100)

    assert stream.getvalue().count("UPLOAD large.bin") == 2
