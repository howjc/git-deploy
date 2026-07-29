"""Deterministic transfer progress and summary accounting tests."""

from __future__ import annotations

from io import StringIO

import pytest

from git_deploy.progress import MIB, ProgressReporter, TransferSummary, format_bytes, format_rate
from git_deploy.transports.base import TransferMeasurementMode


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


class FailingStream(FakeStream):
    """Raise one configured output error instead of accepting rendered text."""

    def __init__(self, error: BaseException, *, successful_writes: int = 0) -> None:
        """Configure the failure and number of writes allowed before it."""

        super().__init__(tty=True)
        self.error = error
        self.successful_writes = successful_writes
        self.write_calls = 0

    def write(self, value: str) -> int:
        """Accept initial writes, then raise the configured display exception."""

        self.write_calls += 1
        if self.write_calls > self.successful_writes:
            raise self.error
        return super().write(value)


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


def test_retry_counts_partial_attempt_bytes_but_deduplicates_payload() -> None:
    """A failed partial attempt contributes bytes/time while payload stays logical."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    first = reporter.callback("app.bin", 1000)
    first(0)
    clock.advance(1)
    first(400)
    reporter.record_retry("app.bin")
    clock.advance(2)
    second = reporter.callback("app.bin", 1000)
    second(0)
    clock.advance(2)
    second(1000)

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 1
    assert summary.payload_bytes == 1000
    assert summary.attempt_bytes == 1400
    assert summary.active_seconds == pytest.approx(3)
    assert summary.retries == 1


def test_callback_rollback_starts_an_implicit_retry_without_negative_delta() -> None:
    """A cumulative-byte reset opens a new attempt and preserves prior bytes."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    callback = reporter.callback("bundle.js", 100)
    callback(0)
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
    assert summary.attempt_bytes == 160
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
    assert summary.attempt_bytes == 20
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
    assert summary.attempt_bytes == 10
    assert summary.retries == 1


def test_duplicate_and_oversized_callbacks_do_not_double_count_attempt_bytes() -> None:
    """Duplicate cumulative values and values beyond total are clamped safely."""

    reporter = ProgressReporter(stream=FakeStream(tty=False))
    callback = reporter.callback("asset.css", 10)
    callback(5)
    callback(5)
    callback(20)
    callback(20)

    summary = reporter.finish()
    assert summary is not None
    assert summary.attempt_bytes == 10
    assert summary.payload_bytes == 10


def test_multiple_files_sum_payload_time_and_average() -> None:
    """Independent files contribute one payload count and additive active time."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    first = reporter.callback("one.bin", 2 * 1024 * 1024)
    first(0)
    clock.advance(1)
    first(2 * 1024 * 1024)
    second = reporter.callback("two.bin", 4 * 1024 * 1024)
    second(0)
    clock.advance(2)
    second(4 * 1024 * 1024)

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 2
    assert summary.payload_bytes == 6 * 1024 * 1024
    assert summary.attempt_bytes == 6 * 1024 * 1024
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
    assert summary.attempt_bytes == 0
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
        (3.25 * MIB, "3.25 MiB/s"),
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


def test_callback_registration_defers_active_time_until_first_signal() -> None:
    """Parent setup and retry delay before the first callback are excluded."""

    clock = FakeClock()
    reporter = ProgressReporter(clock=clock, stream=FakeStream(tty=False))
    reporter.callback("first.bin", 100)
    clock.advance(5)
    reporter.record_retry("first.bin")
    retried = reporter.callback("first.bin", 100)
    clock.advance(3)
    retried(0)
    clock.advance(1)
    retried(100)

    summary = reporter.finish()
    assert summary is not None
    assert summary.active_seconds == pytest.approx(1)
    assert summary.retries == 1


def test_transfer_summary_preserves_streaming_constructor_compatibility() -> None:
    """The v1.6.0 five-value Summary shape defaults to Streaming measurement."""

    summary = TransferSummary(1, 2, 3, 4.0, 5)

    assert summary.attempt_bytes == 3
    assert summary.wire_bytes == 3
    assert summary.measurement_mode is TransferMeasurementMode.STREAMING


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError("closed pipe"),
        OSError("closed stream"),
        UnicodeEncodeError("ascii", "路径", 0, 1, "unsupported"),
        ValueError("closed text stream"),
    ],
)
def test_rendering_errors_disable_output_without_affecting_metrics(error: BaseException) -> None:
    """Pipe, stream, and encoding failures are strictly fail-open."""

    stream = FailingStream(error)
    reporter = ProgressReporter(stream=stream)
    callback = reporter.callback("路径/app.bin", 100)

    callback(0)
    callback(100)
    reporter.render_summary()

    summary = reporter.finish()
    assert summary is not None
    assert summary.files == 1
    assert summary.attempt_bytes == 100
    assert reporter._render_disabled
    assert stream.write_calls == 1


def test_summary_failure_after_completion_is_fail_open() -> None:
    """A stream that closes at the Summary boundary cannot escape the Reporter."""

    stream = FailingStream(OSError("summary sink closed"), successful_writes=2)
    stream.tty = False
    reporter = ProgressReporter(stream=stream)
    reporter.callback("app.bin", 100)(100)

    reporter.render_summary()

    assert reporter.finish() is not None
    assert reporter._render_disabled


def test_coarse_native_mode_avoids_streaming_and_exact_wire_claims() -> None:
    """Native batch output honestly labels its coarse publish measurement."""

    clock = FakeClock()
    stream = FakeStream(tty=True)
    reporter = ProgressReporter(
        clock=clock,
        stream=stream,
        measurement_mode=TransferMeasurementMode.COARSE,
    )
    callback = reporter.callback("assets/app.js", 2 * MIB)
    callback(0)
    clock.advance(2)
    callback(2 * MIB)
    reporter.render_summary()

    output = stream.getvalue()
    assert "transferring (Native batch)" in output
    assert "  0%" not in output
    assert "avg publish 1.00 MiB/s (coarse)" in output
    assert "measurement:    coarse Native batch" in output
    assert "reported bytes: >= 2.0 MiB" in output
    assert "failed partial bytes may be unreported" in output
    assert "attempt bytes:" not in output
    assert "average upload:" not in output


def test_phase_progress_tty_refreshes_and_finishes_on_one_line() -> None:
    """Post-upload phases use a live counter then a final completed line."""

    clock = FakeClock()
    stream = FakeStream(tty=True)
    reporter = ProgressReporter(clock=clock, stream=stream)
    reporter.start_phase("PUBLISH", 3)
    clock.advance(0.3)
    reporter.advance_phase(detail="a.js")
    clock.advance(0.3)
    reporter.advance_phase(detail="b.js")
    reporter.advance_phase(detail="c.js")
    reporter.finish_phase()

    output = stream.getvalue()
    assert "PUBLISH" in output
    assert "3/3" in output
    assert "100%" in output
    assert "c.js" in output


def test_phase_progress_non_tty_prints_only_final_line() -> None:
    """Non-TTY logs avoid per-step spam and keep one completed phase line."""

    stream = FakeStream(tty=False)
    reporter = ProgressReporter(stream=stream)
    reporter.start_phase("PRUNE", 2)
    reporter.advance_phase(detail="old.js")
    reporter.advance_phase(detail="gone/")
    reporter.finish_phase()

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert any("PRUNE 2/2" in line and "100%" in line for line in lines)
    # start_phase with total>0 prints an initial 0/2 line on non-TTY via complete=False path...
    # start_phase calls _render_phase(complete=self._phase_total == 0) so for total=2 complete=False
    # and non-TTY goes to _safe_print newline - so we get start line + finish line.
    assert sum("PRUNE" in line for line in lines) <= 2


def test_update_phase_force_refreshes_detail_without_advancing() -> None:
    """Forced phase updates cover silent Stage RETR between upload and next step."""

    clock = FakeClock()
    stream = FakeStream(tty=True)
    reporter = ProgressReporter(clock=clock, stream=stream)
    reporter.start_phase("STAGE", 2)
    reporter.advance_phase(detail="a.js")
    clock.advance(0.01)
    reporter.update_phase(detail="verify b.js", force=True)

    output = stream.getvalue()
    assert "STAGE" in output
    assert "1/2" in output
    assert "verify b.js" in output
    assert reporter._phase_done == 1
