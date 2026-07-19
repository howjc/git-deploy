"""Transfer progress, retry accounting, and fail-open upload summaries."""

from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TextIO

from git_deploy.transports.base import TransferMeasurementMode

MIB = 1024 * 1024


class TransferAttemptState(str, Enum):
    """Describe whether one physical attempt is registered, active, or complete."""

    REGISTERED = "registered"
    ACTIVE = "active"
    COMPLETED = "completed"


@dataclass(slots=True)
class TransferAttempt:
    """Track one physical upload attempt and its short speed window."""

    started_at: float | None = None
    last_at: float | None = None
    last_transferred: int = 0
    attempt_bytes: int = 0
    state: TransferAttemptState = TransferAttemptState.REGISTERED
    samples: deque[tuple[float, int]] = field(default_factory=deque)


@dataclass(frozen=True, slots=True)
class TransferSummary:
    """Describe logical payload and reported attempt work for one deployment."""

    files: int
    payload_bytes: int
    attempt_bytes: int
    active_seconds: float
    retries: int
    measurement_mode: TransferMeasurementMode = TransferMeasurementMode.STREAMING

    @property
    def wire_bytes(self) -> int:
        """Return the legacy name for application-reported attempt bytes."""

        return self.attempt_bytes

    @property
    def average_bytes_per_second(self) -> float:
        """Return average reported throughput, or zero without elapsed time."""

        if self.active_seconds <= 0:
            return 0.0
        return self.attempt_bytes / self.active_seconds


@dataclass(slots=True)
class ProgressReporter:
    """Render fail-open progress and retain deployment-scoped transfer metrics."""

    verbose: bool = False
    refresh_interval: float = 0.25
    speed_window: float = 1.5
    clock: Callable[[], float] = time.monotonic
    stream: TextIO | None = None
    label: str | None = None
    measurement_mode: TransferMeasurementMode = TransferMeasurementMode.STREAMING
    _attempts: dict[str, TransferAttempt] = field(default_factory=dict, init=False)
    _attempt_counts: dict[str, int] = field(default_factory=dict, init=False)
    _totals: dict[str, int] = field(default_factory=dict, init=False)
    _completed: set[str] = field(default_factory=set, init=False)
    _retry_credits: dict[str, int] = field(default_factory=dict, init=False)
    _file_attempt_bytes: dict[str, int] = field(default_factory=dict, init=False)
    _file_active: dict[str, float] = field(default_factory=dict, init=False)
    _attempt_bytes: int = field(default=0, init=False)
    _active_seconds: float = field(default=0.0, init=False)
    _retries: int = field(default=0, init=False)
    _last_render_at: float = field(default=float("-inf"), init=False)
    _render_width: int = field(default=0, init=False)
    _summary: TransferSummary | None = field(default=None, init=False)
    _finished: bool = field(default=False, init=False)
    _render_disabled: bool = field(default=False, init=False)

    def callback(self, path: str, total: int) -> Callable[[int, int | None], None]:
        """Register one upload without starting its active-time clock.

        Args:
            path: Logical remote path displayed and deduplicated in the summary.
            total: Expected logical byte size, possibly zero.

        Returns:
            A transport-compatible cumulative-byte callback. Its first call
            activates timing after transport-specific setup has completed.
        """

        if self._finished:
            raise RuntimeError("cannot register an upload after transfer reporting finished")
        expected_total = max(0, total)
        known_total = self._totals.setdefault(path, expected_total)
        if known_total != expected_total:
            raise ValueError(f"upload size changed across attempts for {path!r}")
        self._register_attempt(path)
        attempt = self._attempts[path]

        def report(transferred: int, reported_total: int | None = None) -> None:
            """Record cumulative bytes and render a throttled progress line."""

            nonlocal attempt
            effective_total = expected_total
            if reported_total is not None and max(0, reported_total) == expected_total:
                effective_total = max(0, reported_total)
            current = min(effective_total, max(0, transferred))
            if (
                attempt.state is TransferAttemptState.COMPLETED
                and current >= attempt.last_transferred
            ):
                return
            now = self.clock()
            if current < attempt.last_transferred:
                self._close_attempt(path, attempt, now)
                self._retries += 1
                self._completed.discard(path)
                attempt = self._new_attempt(path)
            if attempt.state is TransferAttemptState.REGISTERED:
                self._activate_attempt(attempt, now)
            delta = max(0, current - attempt.last_transferred)
            if delta:
                attempt.attempt_bytes += delta
                self._attempt_bytes += delta
                self._file_attempt_bytes[path] = self._file_attempt_bytes.get(path, 0) + delta
            attempt.last_transferred = current
            attempt.last_at = now
            attempt.samples.append((now, attempt.attempt_bytes))
            self._trim_samples(attempt, now)
            complete = effective_total == 0 or current >= effective_total
            if complete:
                self._close_attempt(path, attempt, now)
                self._completed.add(path)
            if complete or (self._is_tty() and now - self._last_render_at >= self.refresh_interval):
                self._render_progress(path, current, effective_total, attempt, complete=complete)
                self._last_render_at = now

        return report

    def record_retry(self, path: str) -> None:
        """Close a failed attempt and count one scheduled upload retry.

        Args:
            path: Logical path whose next physical upload is a retry.

        Returns:
            ``None`` after preserving partial attempt bytes and active time.
        """

        if self._finished:
            return
        attempt = self._attempts.get(path)
        if attempt is not None and attempt.state is not TransferAttemptState.COMPLETED:
            self._close_attempt(path, attempt, self.clock())
        self._retries += 1
        self._retry_credits[path] = self._retry_credits.get(path, 0) + 1

    def finish(self) -> TransferSummary | None:
        """Freeze and return final metrics without rendering them.

        Returns:
            A summary when at least one upload callback was registered, otherwise ``None``.
        """

        if self._finished:
            return self._summary
        now = self.clock()
        for path, attempt in tuple(self._attempts.items()):
            if attempt.state is not TransferAttemptState.COMPLETED:
                self._close_attempt(path, attempt, now)
        self._finished = True
        if not self._attempt_counts:
            return None
        completed = self._completed
        self._summary = TransferSummary(
            files=len(completed),
            payload_bytes=sum(self._totals[path] for path in completed),
            attempt_bytes=self._attempt_bytes,
            active_seconds=self._active_seconds,
            retries=self._retries,
            measurement_mode=self.measurement_mode,
        )
        return self._summary

    def render_summary(self) -> None:
        """Render the frozen final summary without affecting deployment success.

        Returns:
            ``None`` after writing a stable summary or disabling a failed stream.
        """

        summary = self.finish()
        if summary is None or self._render_disabled:
            return
        heading = "TRANSFER SUMMARY"
        if self.label:
            heading = f"[{self.label}] {heading}"
        self._safe_print(heading)
        if summary.measurement_mode is TransferMeasurementMode.COARSE:
            self._safe_print("  measurement:    coarse Native batch")
        self._safe_print(f"  files:          {summary.files}")
        self._safe_print(f"  payload:        {format_bytes(summary.payload_bytes)}")
        if summary.measurement_mode is TransferMeasurementMode.COARSE:
            self._safe_print(f"  reported bytes: >= {format_bytes(summary.attempt_bytes)}")
        else:
            self._safe_print(f"  attempt bytes:  {format_bytes(summary.attempt_bytes)}")
        self._safe_print(f"  active time:    {summary.active_seconds:.2f}s")
        if summary.measurement_mode is TransferMeasurementMode.COARSE:
            average = f"{format_rate(summary.average_bytes_per_second)} (coarse)"
            self._safe_print(f"  average publish: {average}")
        else:
            average = format_rate(summary.average_bytes_per_second)
            if summary.payload_bytes < MIB or summary.active_seconds < 1.0:
                average = f"{average} (sample too small)"
            else:
                average = (
                    f"{average} "
                    f"({summary.average_bytes_per_second * 8 / 1_000_000:.1f} Mbps)"
                )
            self._safe_print(f"  average upload: {average}")
        self._safe_print(
            f"  retries:        {summary.retries}",
            flush=summary.measurement_mode is TransferMeasurementMode.STREAMING,
        )
        if summary.measurement_mode is TransferMeasurementMode.COARSE:
            self._safe_print("  note:           failed partial bytes may be unreported", flush=True)

    def _register_attempt(self, path: str) -> None:
        """Register a new attempt and avoid double-counting an explicit retry."""

        if self._attempt_counts.get(path, 0):
            credit = self._retry_credits.get(path, 0)
            if credit:
                self._retry_credits[path] = credit - 1
                self._completed.discard(path)
            else:
                previous = self._attempts[path]
                if previous.state is not TransferAttemptState.COMPLETED:
                    self._close_attempt(path, previous, self.clock())
                self._completed.discard(path)
                self._retries += 1
        self._new_attempt(path)

    def _new_attempt(self, path: str) -> TransferAttempt:
        """Create and bind a registered physical attempt for ``path``."""

        attempt = TransferAttempt()
        self._attempts[path] = attempt
        self._attempt_counts[path] = self._attempt_counts.get(path, 0) + 1
        return attempt

    def _activate_attempt(self, attempt: TransferAttempt, now: float) -> None:
        """Start active timing on the transport's first progress signal."""

        attempt.started_at = now
        attempt.last_at = now
        attempt.state = TransferAttemptState.ACTIVE
        attempt.samples.append((now, 0))

    def _close_attempt(self, path: str, attempt: TransferAttempt, now: float) -> None:
        """Accumulate active elapsed time and complete one attempt exactly once."""

        if attempt.state is TransferAttemptState.COMPLETED:
            return
        if attempt.state is TransferAttemptState.ACTIVE and attempt.started_at is not None:
            duration = max(0.0, now - attempt.started_at)
            self._active_seconds += duration
            self._file_active[path] = self._file_active.get(path, 0.0) + duration
        attempt.state = TransferAttemptState.COMPLETED
        attempt.last_at = now

    def _trim_samples(self, attempt: TransferAttempt, now: float) -> None:
        """Keep the speed window plus one boundary sample for stable deltas."""

        cutoff = now - self.speed_window
        while len(attempt.samples) > 2 and attempt.samples[1][0] <= cutoff:
            attempt.samples.popleft()

    def _render_progress(
        self,
        path: str,
        transferred: int,
        total: int,
        attempt: TransferAttempt,
        *,
        complete: bool,
    ) -> None:
        """Render one dynamic or completed upload line through safe output."""

        if self._render_disabled:
            return
        if self.measurement_mode is TransferMeasurementMode.COARSE and not complete:
            line = f"UPLOAD {path} transferring (Native batch)"
        else:
            percent = 100 if total <= 0 else min(100, int(transferred * 100 / total))
            if complete:
                active = self._file_active.get(path, 0.0)
                average = (
                    self._file_attempt_bytes.get(path, 0) / active if active > 0 else 0.0
                )
                suffix = f"avg {format_rate(average)}"
                if self.measurement_mode is TransferMeasurementMode.COARSE:
                    suffix = f"avg publish {format_rate(average)} (coarse)"
                line = f"UPLOAD {path} {percent:3d}%  {format_bytes(total)}  {suffix}"
            else:
                line = (
                    f"UPLOAD {path} {percent:3d}%  {format_bytes(transferred)} / "
                    f"{format_bytes(total)}  {format_rate(self._window_rate(attempt))}"
                )
        tty = self._is_tty()
        if tty and not complete:
            padding = " " * max(0, self._render_width - len(line))
            if self._safe_print(f"\r{line}{padding}", end="", flush=True):
                self._render_width = len(line)
            return
        if tty and self._render_width:
            padding = " " * max(0, self._render_width - len(line))
            if self._safe_print(f"\r{line}{padding}", flush=True):
                self._render_width = 0
            return
        self._safe_print(line, flush=True)

    def _window_rate(self, attempt: TransferAttempt) -> float:
        """Return a guarded sliding-window rate for the active attempt."""

        if len(attempt.samples) < 2:
            return 0.0
        first_time, first_bytes = attempt.samples[0]
        last_time, last_bytes = attempt.samples[-1]
        elapsed = last_time - first_time
        if elapsed <= 0:
            return 0.0
        return max(0, last_bytes - first_bytes) / elapsed

    def _safe_print(self, *values: object, end: str = "\n", flush: bool = False) -> bool:
        """Write display output once, disabling rendering after any stream failure."""

        if self._render_disabled:
            return False
        try:
            print(*values, end=end, file=self._output_stream(), flush=flush)
            return True
        except (OSError, UnicodeError, ValueError):
            # Telemetry is strictly fail-open: a closed pipe or incompatible
            # console encoding must never become an upload or deployment error.
            self._render_disabled = True
            return False

    def _output_stream(self) -> TextIO:
        """Resolve stderr lazily so caller redirection remains effective."""

        return self.stream if self.stream is not None else sys.stderr

    def _is_tty(self) -> bool:
        """Return whether dynamic output is safe, failing open on stream errors."""

        if self._render_disabled:
            return False
        stream = self._output_stream()
        isatty = getattr(stream, "isatty", None)
        if not callable(isatty):
            return False
        try:
            return bool(isatty())
        except (OSError, UnicodeError, ValueError):
            self._render_disabled = True
            return False


def format_bytes(value: int | float) -> str:
    """Format a non-negative byte count with IEC units."""

    amount = max(0.0, float(value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{amount:.0f} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable byte unit")


def format_rate(bytes_per_second: float) -> str:
    """Format a rate directly from its unrounded IEC-unit value."""

    amount = max(0.0, float(bytes_per_second))
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B/s":
                return f"{amount:.0f} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable rate unit")
