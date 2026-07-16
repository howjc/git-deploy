"""Transport factory for the two protocols intentionally supported by v1-lite."""

from __future__ import annotations

from git_deploy.config import TargetConfig
from git_deploy.transports.base import Transport
from git_deploy.transports.ftp import FTPTransport
from git_deploy.transports.openssh_sftp import OpenSSHSFTPTransport, SSHConnectionPool
from git_deploy.transports.sftp import SFTPTransport


def create_transport(
    target: TargetConfig,
    connection_pool: SSHConnectionPool | None = None,
) -> Transport:
    """Create the protocol adapter selected by a target.

    Args:
        target: Validated FTP or SFTP target.

    Returns:
        An unconnected transport instance.
    """

    if target.protocol == "sftp" and target.ssh_host_alias:
        return OpenSSHSFTPTransport(target, connection_pool)
    if target.protocol == "sftp":
        return SFTPTransport(target)
    return FTPTransport(target)


__all__ = ["SSHConnectionPool", "Transport", "create_transport"]
