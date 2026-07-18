"""Public exception hierarchy and CLI exit-code mapping."""

from __future__ import annotations


class GitDeployError(Exception):
    """Represent an expected user-facing git-deploy failure."""

    exit_code = 1


class ConfigError(GitDeployError):
    """Report an invalid or unavailable v1-lite configuration."""

    exit_code = 2


class BuildError(GitDeployError):
    """Report a failed or timed-out local build step."""

    exit_code = 3


class PlanError(GitDeployError):
    """Report an unsafe or impossible local deployment plan."""

    exit_code = 4


class StateError(PlanError):
    """Report unreadable, corrupt, or incompatible lightweight state."""


class DeployError(GitDeployError):
    """Report a remote connection or file-operation failure."""

    exit_code = 5


class StaleRemotePlanError(DeployError):
    """Refuse writes when remote facts changed after the reviewed plan."""
