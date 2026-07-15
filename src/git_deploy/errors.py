"""Domain exceptions and stable CLI exit codes."""


class GitDeployError(Exception):
    """Base error raised for an expected deployment failure."""

    exit_code = 1


class PolicyError(GitDeployError):
    """Report a deployment blocked by a configured safety policy."""

    exit_code = 2


class StalePlanError(PolicyError):
    """Report that a reviewed/signed plan no longer matches execution facts.

    Single stable type for every stale-plan rejection (application token
    mismatch, lock-held domain freshness gate, rollback exact-binding
    drift) so CLI/application error mapping does not depend on matching
    the ``stale_plan`` message substring.
    """


class RemoteDriftError(GitDeployError):
    """Report remote bytes that do not match the declared source commit."""

    exit_code = 3


class ConfigurationError(GitDeployError):
    """Report invalid configuration, paths, or Git revision input."""

    exit_code = 4
