"""Transfer progress, retry accounting, and final upload summaries."""

from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, TextIO

MIB = 1024 * 1024


@dataclass(slots=True)
class TransferAttempt:
    """Track one physical upload attempt and its short speed window."""

    started_at: float
    last_at: float
    last_transferred: int = 0
    wire_bytes: int = 0
    active: bool = True
    samples: deque[tuple[float, int]] = field(default_factory=deque)


@dataclass(frozen=True, slots=True)
class TransferSummary:
    """Describe logical payload and physical transfer work for one deployment."""

    files: int
    payload_bytes: int
    wire_bytes: int
    active_seconds: float
    retries: int

    @property
    def average_bytes_per_second(self) -> float:
        """Return average physical upload throughput, or zero without elapsed time."""

        if self.active_seconds <= 0:
            return 0.0
        return self.wire_bytes / self.active_seconds


@dataclass(slots=True)
class ProgressReporter:
    """Render throttled progress and retain deployment-scoped transfer metrics."""

    verbose: bool = False
    refresh_interval: float = 0.25
    speed_window: float = 1.5
    clock: Callable[[], float] = time.monotonic
    stream: TextIO | None = None
    label: str | None = None
    _attempts: dict[str, TransferAttempt] = field(default_factory=dict, init=False)
    _attempt_counts: dict[str, int] = field(default_factory=dict, init=False)
    _totals: dict[str, int] = field(default_factory=dict, init=False)
    _completed: set[str] = field(default_factory=set, init=False)
    _retry_credits: dict[str, int] = field(default_factory=dict, init=False)
    _file_wire: dict[str, int] = field(default_factory=dict, init=False)
    _file_active: dict[str, float] = field(default_factory=dict, init=False)
    _wire_bytes: int = field(default=0, init=False)
    _active_seconds: float = field(default=0.0, init=False)
    _retries: int = field(default=0, init=False)
    _last_render_at: float = field(default=float("-inf"), init=False)
    _render_width: int = field(default=0, init=False)
    _summary: TransferSummary | None = field(default=None, init=False)
    _finished: bool = field(default=False, init=False)

    def callback(self, path: str, total: int) -> Callable[[int, int | None], None]:
        """Create a cumulative-byte callback for one physical upload attempt.

        Args:
            path: Logical remote path displayed and deduplicated in the summary.
            total: Expected logical byte size, possibly zero.

        Returns:
            A transport-compatible ``(transferred, total)`` callback.
        """

        if self._finished:
            raise RuntimeError("cannot register an upload after transfer reporting finished")
        expected_total = max(0, total)
        known_total = self._totals.setdefault(path, expected_total)
        if known_total != expected_total:
            raise ValueError(f"upload size changed across attempts for {path!r}")
        self._start_attempt(path)
        attempt = self._attempts[path]

        def report(transferred: int, reported_total: int | None = None) -> None:
            """Record cumulative bytes and render a throttled progress line."""

            nonlocal attempt
            effective_total = expected_total
            if reported_total is not None:
                effective_total = max(0, reported_total)
                if effective_total != expected_total:
                    effective_total = expected_total
            current = min(effective_total, max(0, transferred))
            now = self.clock()
            if not attempt.active and current >= attempt.last_transferred:
                return
            if current < attempt.last_transferred:
                self._close_attempt(path, attempt, now)
                self._retries += 1
                self._completed.discard(path)
                attempt = self._new_attempt(path, now)
            delta = max(0, current - attempt.last_transferred)
            if delta:
                attempt.wire_bytes += delta
                self._wire_bytes += delta
                self._file_wire[path] = self._file_wire.get(path, 0) + delta
            attempt.last_transferred = current
            attempt.last_at = now
            attempt.samples.append((now, attempt.wire_bytes))
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
            ``None`` after preserving partial wire bytes and active time.
        """

        if self._finished:
            return
        attempt = self._attempts.get(path)
        if attempt is not None and attempt.active:
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
            if attempt.active:
                self._close_attempt(path, attempt, now)
        self._finished = True
        if not self._attempt_counts:
            return None
        completed = self._completed
        self._summary = TransferSummary(
            files=len(completed),
            payload_bytes=sum(self._totals[path] for path in completed),
            wire_bytes=self._wire_bytes,
            active_seconds=self._active_seconds,
            retries=self._retries,
        )
        return self._summary

    def render_summary(self) -> None:
        """Render the frozen final summary when uploads occurred.

        Returns:
            ``None`` after writing a stable multi-line summary.
        """

        summary = self.finish()
        if summary is None:
            return
        stream = self._output_stream()
        heading = "TRANSFER SUMMARY"
        if self.label:
            heading = f"[{self.label}] {heading}"
        average = format_rate(summary.average_bytes_per_second)
        if summary.payload_bytes < MIB or summary.active_seconds < 1.0:
            average = f"{average} (sample too small)"
        else:
            average = f"{average} ({summary.average_bytes_per_second * 8 / 1_000_000:.1f} Mbps)"
        print(heading, file=stream)
        print(f"  files:          {summary.files}", file=stream)
        print(f"  payload:        {format_bytes(summary.payload_bytes)}", file=stream)
        print(f"  wire bytes:     {format_bytes(summary.wire_bytes)}", file=stream)
        print(f"  active time:    {summary.active_seconds:.2f}s", file=stream)
        print(f"  average upload: {average}", file=stream)
        print(f"  retries:        {summary.retries}", file=stream, flush=True)

    def _start_attempt(self, path: str) -> None:
        """Register a new attempt and avoid double-counting an explicit retry."""

        if self._attempt_counts.get(path, 0):
            credit = self._retry_credits.get(path, 0)
            if credit:
                self._retry_credits[path] = credit - 1
                self._completed.discard(path)
            else:
                previous = self._attempts[path]
                if previous.active:
                    self._close_attempt(path, previous, self.clock())
                self._completed.discard(path)
                self._retries += 1
        self._new_attempt(path, self.clock())

    def _new_attempt(self, path: str, now: float) -> TransferAttempt:
        """Create and bind an empty physical attempt for ``path``."""

        attempt = TransferAttempt(now, now)
        attempt.samples.append((now, 0))
        self._attempts[path] = attempt
        self._attempt_counts[path] = self._attempt_counts.get(path, 0) + 1
        return attempt

    def _close_attempt(self, path: str, attempt: TransferAttempt, now: float) -> None:
        """Accumulate one attempt's elapsed upload time exactly once."""

        if not attempt.active:
            return
        duration = max(0.0, now - attempt.started_at)
        attempt.active = False
        attempt.last_at = now
        self._active_seconds += duration
        self._file_active[path] = self._file_active.get(path, 0.0) + duration

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
        """Render one dynamic or completed upload line."""

        percent = 100 if total <= 0 else min(100, int(transferred * 100 / total))
        if complete:
            active = self._file_active.get(path, 0.0)
            average = self._file_wire.get(path, 0) / active if active > 0 else 0.0
            line = (
                f"UPLOAD {path} {percent:3d}%  {format_bytes(total)}  "
                f"avg {format_rate(average)}"
            )
        else:
            line = (
                f"UPLOAD {path} {percent:3d}%  {format_bytes(transferred)} / "
                f"{format_bytes(total)}  {format_rate(self._window_rate(attempt))}"
            )
        stream = self._output_stream()
        if self._is_tty() and not complete:
            padding = " " * max(0, self._render_width - len(line))
            print(f"\r{line}{padding}", end="", file=stream, flush=True)
            self._render_width = len(line)
            return
        if self._is_tty() and self._render_width:
            padding = " " * max(0, self._render_width - len(line))
            print(f"\r{line}{padding}", file=stream, flush=True)
            self._render_width = 0
            return
        print(line, file=stream, flush=True)

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

    def _output_stream(self) -> TextIO:
        """Resolve stderr lazily so pytest and caller redirection remain effective."""

        return self.stream if self.stream is not None else sys.stderr

    def _is_tty(self) -> bool:
        """Return whether dynamic carriage-return output is safe."""

        stream = self._output_stream()
        isatty = getattr(stream, "isatty", None)
        return bool(isatty()) if callable(isatty) else False


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
    """Format a non-negative byte-per-second value with IEC units."""

    rendered = format_bytes(bytes_per_second)
    if rendered.endswith(" B"):
        return f"{rendered}/s"
    value, unit = rendered.split(" ", 1)
    return f"{float(value):.2f} {unit}/s"
