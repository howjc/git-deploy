"""Internal exec shim that removes 1Password authentication before a build."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Strip every ``OP_*`` variable and replace this process with literal argv."""

    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments:
        raise SystemExit("secret exec requires a command after --")
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("OP_")
    }
    os.execvpe(arguments[0], arguments, environment)


if __name__ == "__main__":
    main()
