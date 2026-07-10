"""Domain exceptions and stable CLI exit codes."""


class GitDeployError(Exception):
    """Base error raised for an expected deployment failure."""

    exit_code = 1


class PolicyError(GitDeployError):
    """Report a deployment blocked by a configured safety policy."""

    exit_code = 2


class RemoteDriftError(GitDeployError):
    """Report remote bytes that do not match the declared source commit."""

    exit_code = 3


class ConfigurationError(GitDeployError):
    """Report invalid configuration, paths, or Git revision input."""

    exit_code = 4
