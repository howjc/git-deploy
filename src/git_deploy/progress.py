"""Structured deployment progress events and terminal rendering."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class ProgressEvent:
    """One progress snapshot emitted by the deployment executor."""

    phase: str
    completed: int
    total: int
    path: str = ""
    bytes_completed: int = 0
    bytes_total: int = 0


class TerminalProgress:
    """Render progress compactly on terminals and periodically in logs."""

    def __init__(self, project: str, stream: TextIO | None = None):
        """Initialize a project-scoped terminal renderer.

        Args:
            project: Configured project name shown in each line.
            stream: Output stream; defaults to standard error.
        """

        self.project = project
        self.stream = stream or sys.stderr
        self._is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_width = 0
        self._active_phase = ""
        self._log_bucket: dict[str, int] = {}

    def update(self, event: ProgressEvent) -> None:
        """Render one executor progress event.

        Args:
            event: Current phase, file count, path, and byte counters.
        """

        message = self._format(event)
        if self._is_tty:
            self._render_tty(message, event)
        elif self._should_log(event):
            print(message, file=self.stream, flush=True)

    def finish(self) -> None:
        """Terminate an active TTY line before normal output resumes."""

        if self._is_tty and self._active_phase:
            self.stream.write("\n")
            self.stream.flush()
        self._active_phase = ""
        self._last_width = 0

    def _format(self, event: ProgressEvent) -> str:
        """Build one width-constrained progress line.

        Args:
            event: Progress snapshot to format.

        Returns:
            Human-readable progress text.
        """

        count = f"{event.completed}/{event.total}" if event.total else str(event.completed)
        parts = [f"[{self.project}]", f"{event.phase.upper():<8}", count]
        if event.bytes_total:
            parts.append(
                f"{_human_bytes(event.bytes_completed)}/{_human_bytes(event.bytes_total)}"
            )
        elif event.bytes_completed:
            parts.append(_human_bytes(event.bytes_completed))
        if event.path:
            parts.append(event.path)
        text = "  ".join(parts)
        width = max(40, shutil.get_terminal_size((100, 20)).columns - 1)
        if len(text) > width:
            text = text[: max(0, width - 3)] + "..."
        return text

    def _render_tty(self, message: str, event: ProgressEvent) -> None:
        """Refresh one interactive terminal line.

        Args:
            message: Fully formatted line.
            event: Event used to detect phase completion.
        """

        if self._active_phase and self._active_phase != event.phase:
            self.stream.write("\n")
            self._last_width = 0
        padding = " " * max(0, self._last_width - len(message))
        self.stream.write(f"\r{message}{padding}")
        self.stream.flush()
        self._active_phase = event.phase
        self._last_width = len(message)
        if event.total and event.completed >= event.total:
            self.stream.write("\n")
            self.stream.flush()
            self._active_phase = ""
            self._last_width = 0

    def _should_log(self, event: ProgressEvent) -> bool:
        """Rate-limit non-interactive progress to roughly ten lines per phase.

        Args:
            event: Current progress snapshot.

        Returns:
            Whether this event should become a log line.
        """

        if event.total <= 0:
            return event.completed == 0
        if event.bytes_total > 0:
            numerator = event.bytes_completed
            denominator = event.bytes_total
        else:
            numerator = event.completed
            denominator = event.total
        bucket = min(10, int(numerator * 10 / max(1, denominator)))
        previous = self._log_bucket.get(event.phase, -1)
        if bucket <= previous and event.completed < event.total:
            return False
        self._log_bucket[event.phase] = bucket
        return True


def _human_bytes(value: int) -> str:
    """Format a byte count using compact binary units.

    Args:
        value: Non-negative byte count.

    Returns:
        Compact human-readable size.
    """

    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"
