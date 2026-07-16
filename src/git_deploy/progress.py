"""Small terminal progress renderer shared by FTP and SFTP uploads."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(slots=True)
class ProgressReporter:
    """Render byte progress without retaining deployment history."""

    verbose: bool = False

    def callback(self, path: str, total: int):  # noqa: ANN201
        """Create a transport callback for one upload.

        Args:
            path: Remote relative path displayed to the user.
            total: Expected byte size, possibly zero.

        Returns:
            A ``(transferred, total)`` compatible progress callback.
        """

        last_percent = -1

        def report(transferred: int, reported_total: int | None = None) -> None:
            """Print progress at useful percentage boundaries."""

            nonlocal last_percent
            effective_total = reported_total if reported_total is not None else total
            percent = 100 if effective_total <= 0 else min(100, int(transferred * 100 / effective_total))
            if self.verbose or percent == 100 or percent >= last_percent + 10:
                print(f"\rUPLOAD {path}: {percent:3d}%", end="", file=sys.stderr, flush=True)
                last_percent = percent
                if percent == 100:
                    print(file=sys.stderr)

        return report
