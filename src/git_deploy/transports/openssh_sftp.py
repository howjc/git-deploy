"""Native OpenSSH SFTP backend with private ControlMaster connection reuse."""

from __future__ import annotations

import hashlib
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from git_deploy.config import TargetConfig, resolve_current_ssh_alias
from git_deploy.errors import DeployError
from git_deploy.transports.base import ProgressCallback, RemotePathType, Transport


@dataclass(frozen=True, slots=True)
class OpenSSHEndpointKey:
    """Identify one reusable native OpenSSH endpoint without remote-root coupling."""

    ssh: str
    sftp: str
    config_file: str | None
    alias: str
    host: str
    username: str
    port: int


class PathProbeResult(Enum):
    """Classify one native SFTP path probe without hiding remote errors."""

    EXISTS = "exists"
    MISSING = "missing"
    ERROR = "error"


class OpenSSHMaster:
    """Own one short-lived OpenSSH ControlMaster and its private socket directory."""

    def __init__(self, target: TargetConfig) -> None:
        """Resolve executables and prepare an unconnected master.

        Args:
            target: Frozen SFTP target retaining its OpenSSH alias.
        """

        if not target.ssh_host_alias:
            raise DeployError("Native OpenSSH backend requires ssh_host_alias")
        self.target = target
        self.ssh = _find_posix_executable("ssh")
        self.sftp = _find_posix_executable("sftp")
        self.alias = target.ssh_host_alias
        self.directory: Path | None = None
        self.control_path: Path | None = None
        self.connected = False

    @property
    def key(self) -> OpenSSHEndpointKey:
        """Return the connection-pool identity excluding remote root."""

        if not self.target.ssh_resolved or not self.target.host or not self.target.username:
            raise DeployError("Native OpenSSH backend requires a frozen resolved target")
        config = (
            str(self.target.ssh_config_file)
            if self.target.ssh_config_explicit or self.target.ssh_config_file.is_file()
            else None
        )
        return OpenSSHEndpointKey(
            self.ssh,
            self.sftp,
            config,
            self.alias,
            self.target.host,
            self.target.username,
            self.target.port,
        )

    def connect(self) -> None:
        """Establish one interactive-capable background ControlMaster."""

        if self.connected:
            return
        self._assert_alias_current()
        root = self.target.runtime_dir or Path(tempfile.gettempdir()) / "git-deploy"
        socket_root = _control_socket_root(root, self.target.name)
        socket_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(socket_root, 0o700)
        directory = Path(
            tempfile.mkdtemp(prefix=f"{_safe_name(self.target.name)}-", dir=socket_root)
        )
        os.chmod(directory, 0o700)
        control_path = directory / "control.sock"
        command = [
            self.ssh,
            *self._config_arguments(),
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPath={control_path}",
            "-o",
            "ControlPersist=60",
            *self._pinned_endpoint_arguments(),
            "-MNf",
            self.alias,
        ]
        try:
            result = subprocess.run(
                command,
                stdin=None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise DeployError(
                f"OpenSSH authentication failed for target {self.target.name}: {exc}"
            ) from exc
        except BaseException:
            # Ctrl-C and other cancellation paths bypass ordinary Exception
            # handlers; remove the private directory before preserving them.
            shutil.rmtree(directory, ignore_errors=True)
            raise
        if result.returncode != 0:
            shutil.rmtree(directory, ignore_errors=True)
            raise DeployError(
                f"OpenSSH authentication failed for target {self.target.name} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        self.directory = directory
        self.control_path = control_path
        self.connected = True
        check = self.control_command("check")
        if check.returncode != 0:
            detail = check.stderr.strip()
            self.close()
            raise DeployError(f"OpenSSH master did not become ready: {detail}")

    def control_command(self, operation: str) -> subprocess.CompletedProcess[str]:
        """Run an OpenSSH control operation against this master."""

        if self.control_path is None:
            return subprocess.CompletedProcess([], 1, "", "master is not connected")
        return subprocess.run(
            [
                self.ssh,
                *self._config_arguments(),
                "-o",
                f"ControlPath={self.control_path}",
                *self._pinned_endpoint_arguments(),
                "-O",
                operation,
                self.alias,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def is_healthy(self) -> bool:
        """Return whether OpenSSH still recognizes the cached master.

        Returns:
            ``True`` only after a successful local control-socket check.
        """

        if not self.connected:
            return False
        try:
            return self.control_command("check").returncode == 0
        except OSError:
            return False

    def run_batch(
        self,
        commands: tuple[str, ...],
        *,
        operation: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run SFTP batch commands through the existing master connection."""

        if not self.connected or self.control_path is None:
            raise DeployError("OpenSSH master is not connected")
        payload = "\n".join(commands) + "\n"
        result = subprocess.run(
            [
                self.sftp,
                *self._config_arguments(),
                "-q",
                "-b",
                "-",
                "-o",
                f"ControlPath={self.control_path}",
                *self._pinned_endpoint_arguments(),
                self.alias,
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
        if check and result.returncode != 0:
            raise DeployError(
                f"OpenSSH SFTP {operation} failed for target {self.target.name} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        return result

    def run_command(
        self,
        command: str,
        cwd: PurePosixPath,
        timeout: float | None,
    ) -> None:
        """Run one non-interactive command through the existing master.

        Args:
            command: Validated one-line shell command.
            cwd: Absolute remote working directory.
            timeout: Optional whole-command timeout in seconds.

        Returns:
            ``None`` after a zero remote exit status.
        """

        if not self.connected or self.control_path is None:
            raise DeployError("OpenSSH master is not connected")
        wrapped = f"cd -- {shlex.quote(cwd.as_posix())} && {command}"
        invocation = [
            self.ssh,
            *self._config_arguments(),
            "-o",
            f"ControlPath={self.control_path}",
            *self._pinned_endpoint_arguments(),
            "-T",
            self.alias,
            wrapped,
        ]
        try:
            result = subprocess.run(
                invocation,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployError(
                f"remote command timed out after {timeout} second(s): {command}"
            ) from exc
        except OSError as exc:
            raise DeployError(f"cannot execute remote command: {exc}") from exc
        if result.returncode != 0:
            raise DeployError(
                f"remote command failed with exit={result.returncode}: {command}"
            )

    def run_capture(self, command: str) -> str:
        """Run one non-interactive read-only probe and return text stdout.

        Args:
            command: Fully quoted POSIX shell probe.

        Returns:
            Exact stdout after a zero remote exit status.
        """

        if not self.connected or self.control_path is None:
            raise DeployError("OpenSSH master is not connected")
        invocation = [
            self.ssh,
            *self._config_arguments(),
            "-o",
            f"ControlPath={self.control_path}",
            *self._pinned_endpoint_arguments(),
            "-T",
            self.alias,
            command,
        ]
        try:
            result = subprocess.run(
                invocation,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.target.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeployError(f"cannot execute remote path probe: {exc}") from exc
        if result.returncode != 0:
            raise DeployError(
                f"remote path probe failed with exit={result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout

    def close(self) -> None:
        """Close the master and delete its private directory idempotently."""

        if self.connected:
            try:
                self.control_command("exit")
            except OSError:
                pass
        self.connected = False
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory = None
        self.control_path = None

    def _config_arguments(self) -> list[str]:
        """Return the explicit OpenSSH config selector when configured."""

        if self.target.ssh_config_explicit or self.target.ssh_config_file.is_file():
            return ["-F", str(self.target.ssh_config_file)]
        return []

    def _pinned_endpoint_arguments(self) -> list[str]:
        """Keep alias policy while pinning the endpoint approved in the plan.

        Returns:
            OpenSSH options for frozen host/user/port and connect timeout.
        """

        if not self.target.host or not self.target.username:
            raise DeployError("Native OpenSSH target lacks a frozen host or username")
        return [
            "-o",
            f"HostName={self.target.host}",
            "-o",
            f"User={self.target.username}",
            "-o",
            f"Port={self.target.port}",
            "-o",
            f"ConnectTimeout={max(1, math.ceil(self.target.timeout))}",
        ]

    def _assert_alias_current(self) -> None:
        """Reject SSH config drift between plan review and real connection.

        Returns:
            ``None`` only when current and approved endpoints are identical.
        """

        try:
            current = resolve_current_ssh_alias(
                self.target,
                ssh_executable=self.ssh,
            )
        except Exception as exc:
            raise DeployError(
                f"stale target: cannot re-resolve SSH alias {self.alias!r}; re-run required: {exc}"
            ) from exc
        frozen = (self.target.host, self.target.username, self.target.port)
        observed = (current.host, current.username, current.port)
        if observed != frozen:
            raise DeployError(
                "stale target: SSH alias changed after plan; re-run required "
                f"(approved {frozen[1]}@{frozen[0]}:{frozen[2]}, "
                f"now {observed[1]}@{observed[0]}:{observed[2]})"
            )


class SSHConnectionPool:
    """Reuse native OpenSSH masters within one foreground command."""

    def __init__(self) -> None:
        """Initialize an empty command-scoped connection pool."""

        self._masters: dict[OpenSSHEndpointKey, OpenSSHMaster] = {}

    def acquire(self, target: TargetConfig) -> OpenSSHMaster:
        """Return one healthy endpoint master, replacing a dead cached one.

        Args:
            target: Prepared Native OpenSSH target requesting a connection.

        Returns:
            Reused healthy or newly established endpoint master.
        """

        candidate = OpenSSHMaster(target)
        key = candidate.key
        existing = self._masters.get(key)
        if existing is not None:
            if existing.is_healthy():
                return existing
            self.invalidate(existing)
        candidate.connect()
        self._masters[key] = candidate
        return candidate

    def invalidate(self, master: OpenSSHMaster) -> None:
        """Evict and close one failed master by identity.

        Args:
            master: Cached connection that must never be reused.

        Returns:
            ``None`` after eviction and cleanup.
        """

        for key, existing in tuple(self._masters.items()):
            if existing is master:
                self._masters.pop(key, None)
                break
        master.close()

    def close_all(self) -> None:
        """Close every pooled master and clear the pool idempotently."""

        masters = tuple(self._masters.values())
        self._masters.clear()
        for master in masters:
            master.close()


class OpenSSHSFTPTransport(Transport):
    """Perform SFTP file operations through the user's native OpenSSH environment."""

    def __init__(
        self,
        target: TargetConfig,
        connection_pool: SSHConnectionPool | None = None,
    ) -> None:
        """Create a native SFTP transport with optional workspace connection reuse."""

        if not target.ssh_host_alias:
            raise DeployError("Native OpenSSH backend requires ssh_host_alias")
        self.target = target
        self.pool = connection_pool
        self.master: OpenSSHMaster | None = None
        self._owns_master = connection_pool is None

    def connect(self) -> None:
        """Acquire or establish one native OpenSSH master connection."""

        if self.master is not None:
            return
        if self.pool is not None:
            self.master = self.pool.acquire(self.target)
        else:
            master = OpenSSHMaster(self.target)
            master.connect()
            self.master = master

    def root_exists(self) -> bool:
        """Check the configured remote root without creating it."""

        return self._probe(self.target.remote_root.as_posix()) is PathProbeResult.EXISTS

    def ensure_root(self) -> None:
        """Create every missing remote-root component through SFTP batch mode."""

        self._mkdirs(self.target.remote_root.as_posix())

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Upload, chmod, and safely publish one file through the shared master."""

        target = self._absolute(remote_path)
        self._mkdirs(PurePosixPath(target).parent.as_posix())
        temporary = f"{target}.git-deploy-{uuid.uuid4().hex}.tmp"
        master = self._require_master()
        try:
            callback(0, local_path.stat().st_size)
            master.run_batch(
                (
                    f"put {_quote_sftp(str(local_path))} {_quote_sftp(temporary)}",
                    f"chmod {'0755' if executable else '0644'} {_quote_sftp(temporary)}",
                ),
                operation=f"upload {remote_path}",
            )
            self._publish_temporary(temporary, target)
            size = local_path.stat().st_size
            callback(size, size)
        except Exception:
            master.run_batch(
                (f"-rm {_quote_sftp(temporary)}",),
                operation=f"cleanup {remote_path}",
                check=False,
            )
            raise

    def delete(self, remote_path: str) -> None:
        """Delete an owned file while treating a confirmed absence as success."""

        target = self._absolute(remote_path)
        if self._probe(target) is PathProbeResult.MISSING:
            return
        self._require_master().run_batch(
            (f"rm {_quote_sftp(target)}",),
            operation=f"delete {remote_path}",
        )

    def run_command(
        self,
        command: str,
        *,
        cwd: PurePosixPath,
        timeout: float | None,
    ) -> None:
        """Execute one command through the already authenticated ControlMaster.

        Args:
            command: Validated one-line shell command.
            cwd: Absolute remote working directory.
            timeout: Optional whole-command timeout in seconds.

        Returns:
            ``None`` after a zero remote exit status.
        """

        self._require_master().run_command(command, cwd, timeout)

    def lstat(self, remote_path: str) -> RemotePathType:
        """Classify one relative path through the existing pinned SSH master.

        Args:
            remote_path: Safe relative path below ``remote_root``.

        Returns:
            Confirmed path type without following a symlink.
        """

        absolute = self._absolute(remote_path)
        quoted = shlex.quote(absolute)
        output = self._require_master().run_capture(
            f"p={quoted}; if [ -L \"$p\" ]; then printf symlink; "
            "elif [ -f \"$p\" ]; then printf file; "
            "elif [ -d \"$p\" ]; then printf directory; "
            "elif [ -e \"$p\" ]; then printf other; else printf missing; fi"
        ).strip()
        try:
            return RemotePathType(output)
        except ValueError as exc:
            raise DeployError(f"remote path probe returned an invalid type: {output!r}") from exc

    def read_file(self, remote_path: str, *, max_bytes: int) -> bytes:
        """Download one bounded regular metadata file through the active master."""

        if self.lstat(remote_path) is not RemotePathType.FILE:
            raise DeployError(f"remote metadata path is not a regular file: {remote_path}")
        local: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="git-deploy-read-", delete=False) as handle:
                local = Path(handle.name)
            self._require_master().run_batch(
                (f"get {_quote_sftp(self._absolute(remote_path))} {_quote_sftp(str(local))}",),
                operation=f"read metadata {remote_path}",
            )
            if local.stat().st_size > max_bytes:
                raise DeployError(f"remote metadata file exceeds {max_bytes} bytes: {remote_path}")
            return local.read_bytes()
        except DeployError:
            raise
        except OSError as exc:
            raise DeployError(f"cannot read remote metadata {remote_path}: {exc}") from exc
        finally:
            if local is not None:
                local.unlink(missing_ok=True)

    def write_file_atomic(self, remote_path: str, data: bytes) -> None:
        """Publish metadata via the existing safe Native upload path."""

        local: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="git-deploy-write-", delete=False) as handle:
                handle.write(data)
                local = Path(handle.name)
            self.upload(local, remote_path, lambda done, total: None)
        except DeployError:
            raise
        except OSError as exc:
            raise DeployError(f"cannot stage remote metadata {remote_path}: {exc}") from exc
        finally:
            if local is not None:
                local.unlink(missing_ok=True)

    def list_directory(self, remote_path: str) -> tuple[str, ...]:
        """List stable direct names for one Native SFTP directory."""

        kind = self.lstat(remote_path)
        if kind is RemotePathType.MISSING:
            return ()
        if kind is not RemotePathType.DIRECTORY:
            raise DeployError(f"remote path is not a directory: {remote_path}")
        absolute = self._absolute(remote_path).rstrip("/")
        result = self._require_master().run_batch(
            (f"ls -1 {_quote_sftp(absolute)}",),
            operation=f"list directory {remote_path}",
        )
        names_list: list[str] = []
        prefix = absolute + "/"
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if not line or line.startswith("sftp>"):
                continue
            names_list.append(line[len(prefix) :] if line.startswith(prefix) else line)
        names = tuple(sorted(names_list))
        if any(name in {".", ".."} or "/" in name or "\\" in name for name in names):
            raise DeployError(f"remote directory contains an unsafe name: {remote_path}")
        return names

    def make_directory(self, remote_path: str, *, mode: int = 0o755) -> None:
        """Create a safe relative remote directory recursively."""

        absolute = self._absolute(remote_path)
        self._mkdirs(absolute, mode=mode)
        self._require_master().run_batch(
            (f"chmod {mode:04o} {_quote_sftp(absolute)}",),
            operation=f"chmod directory {remote_path}",
        )

    def rename_path(self, source: str, destination: str) -> None:
        """Rename one Native SFTP path without overwriting a destination."""

        if self.lstat(destination) is not RemotePathType.MISSING:
            raise DeployError(f"remote rename destination already exists: {destination}")
        self._require_master().run_batch(
            (
                f"rename {_quote_sftp(self._absolute(source))} "
                f"{_quote_sftp(self._absolute(destination))}",
            ),
            operation=f"rename {source} to {destination}",
        )

    def remove_tree(self, remote_path: str) -> None:
        """Recursively remove one Native SFTP tree without following symlinks."""

        kind = self.lstat(remote_path)
        if kind is RemotePathType.MISSING:
            return
        absolute = self._absolute(remote_path)
        if kind is RemotePathType.DIRECTORY:
            for name in self.list_directory(remote_path):
                self.remove_tree((PurePosixPath(remote_path) / name).as_posix())
            self._require_master().run_batch(
                (f"rmdir {_quote_sftp(absolute)}",),
                operation=f"remove directory {remote_path}",
            )
        elif kind in {RemotePathType.FILE, RemotePathType.SYMLINK}:
            self._require_master().run_batch(
                (f"rm {_quote_sftp(absolute)}",),
                operation=f"remove path {remote_path}",
            )
        else:
            raise DeployError(f"refusing to remove unsupported remote type: {remote_path}")

    def close(self) -> None:
        """Release an owned master; pooled masters live until pool close_all()."""

        master = self.master
        self.master = None
        if master is not None and self._owns_master:
            master.close()

    def invalidate_connection(self) -> None:
        """Evict a failed pooled master so retries create a new connection.

        Returns:
            ``None`` after the failed connection is no longer reusable.
        """

        master = self.master
        self.master = None
        if master is None:
            return
        if self.pool is not None:
            self.pool.invalidate(master)
        else:
            master.close()

    def _publish_temporary(self, temporary: str, target: str) -> None:
        """Prefer direct rename, then use a recoverable backup swap."""

        master = self._require_master()
        direct = master.run_batch(
            (f"rename {_quote_sftp(temporary)} {_quote_sftp(target)}",),
            operation=f"publish {target}",
            check=False,
        )
        if direct.returncode == 0:
            return
        if self._probe(target) is PathProbeResult.MISSING:
            raise DeployError(
                f"OpenSSH SFTP publish failed for {target}: {direct.stderr.strip()}"
            )
        backup = f"{target}.git-deploy-{uuid.uuid4().hex}.bak"
        master.run_batch(
            (f"rename {_quote_sftp(target)} {_quote_sftp(backup)}",),
            operation=f"backup {target}",
        )
        published = master.run_batch(
            (f"rename {_quote_sftp(temporary)} {_quote_sftp(target)}",),
            operation=f"publish {target}",
            check=False,
        )
        if published.returncode != 0:
            restored = master.run_batch(
                (f"rename {_quote_sftp(backup)} {_quote_sftp(target)}",),
                operation=f"restore {target}",
                check=False,
            )
            if restored.returncode != 0:
                raise DeployError(
                    f"OpenSSH publish and backup restore both failed for {target}: "
                    f"{restored.stderr.strip()}"
                )
            raise DeployError(f"OpenSSH SFTP publish failed for {target}: {published.stderr.strip()}")
        master.run_batch(
            (f"-rm {_quote_sftp(backup)}",),
            operation=f"cleanup backup {target}",
            check=False,
        )

    def _mkdirs(self, absolute: str, *, mode: int = 0o755) -> None:
        """Create missing absolute directory components one at a time."""

        current = PurePosixPath("/")
        for component in PurePosixPath(absolute).parts[1:]:
            current /= component
            value = current.as_posix()
            if self._probe(value) is PathProbeResult.MISSING:
                self._require_master().run_batch(
                    (
                        f"mkdir {_quote_sftp(value)}",
                        f"chmod {mode:04o} {_quote_sftp(value)}",
                    ),
                    operation=f"create directory {value}",
                )

    def _probe(self, absolute: str) -> PathProbeResult:
        """Return exists/missing and raise for every ambiguous probe failure.

        Args:
            absolute: Absolute remote path inspected through SFTP.

        Returns:
            ``EXISTS`` or confirmed ``MISSING``; ``ERROR`` raises instead.
        """

        result = self._require_master().run_batch(
            (f"ls {_quote_sftp(absolute)}",),
            operation=f"inspect {absolute}",
            check=False,
        )
        detail = "\n".join((result.stdout, result.stderr)).strip()
        classification = _classify_path_probe(result.returncode, detail, absolute)
        if classification is not PathProbeResult.ERROR:
            return classification
        raise DeployError(
            f"OpenSSH SFTP inspect failed for target {self.target.name} "
            f"at {absolute} (exit {result.returncode}): {detail or '<no error detail>'}"
        )

    def _absolute(self, relative: str) -> str:
        """Join a safe relative operation path below the configured root."""

        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise DeployError(f"unsafe OpenSSH SFTP relative path: {relative!r}")
        return (self.target.remote_root / path).as_posix()

    def _require_master(self) -> OpenSSHMaster:
        """Return the active master or fail clearly."""

        if self.master is None:
            raise DeployError("Native OpenSSH transport is not connected")
        return self.master


def _find_posix_executable(name: str) -> str:
    """Find a system OpenSSH executable while refusing Windows ``.exe`` paths."""

    value = shutil.which(name)
    if value is None:
        raise DeployError(f"Native OpenSSH backend requires the system '{name}' executable")
    path = Path(value)
    if path.suffix.lower() == ".exe" or not path.is_absolute():
        raise DeployError(
            f"Native OpenSSH backend requires a POSIX {name} executable, found: {value}"
        )
    return str(path)


def _quote_sftp(value: str) -> str:
    """Quote one SFTP batch argument and reject command-injection delimiters."""

    if any(character in value for character in ("\0", "\n", "\r")):
        raise DeployError("SFTP paths must not contain NUL or newline characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_confirmed_missing(detail: str, remote_path: str) -> bool:
    """Recognize remote-path absence without swallowing a missing control socket.

    Args:
        detail: C-locale stdout/stderr from the failed SFTP batch.
        remote_path: Exact remote path whose existence was queried.

    Returns:
        ``True`` only for an explicit diagnostic tied to the remote path.
    """

    path = remote_path.lower()
    for raw_line in detail.splitlines():
        line = " ".join(raw_line.strip().split()).lower()
        if "no such file or directory" in line:
            if "couldn't stat remote file:" in line:
                return True
            if path in line and line.startswith(("ls ", "stat ")):
                return True
        if (
            path in line
            and line.endswith("not found")
            and line.startswith(("can't ls:", "can't stat:"))
        ):
            return True
    return False


def _classify_path_probe(
    returncode: int,
    detail: str,
    remote_path: str,
) -> PathProbeResult:
    """Map one SFTP result to exists, confirmed missing, or ambiguous error.

    Args:
        returncode: SFTP process exit status.
        detail: Combined C-locale diagnostic output.
        remote_path: Exact path queried by the batch.

    Returns:
        One explicit three-state probe classification.
    """

    if returncode == 0:
        return PathProbeResult.EXISTS
    if _is_confirmed_missing(detail, remote_path):
        return PathProbeResult.MISSING
    return PathProbeResult.ERROR


def _safe_name(value: str) -> str:
    """Return a filesystem-safe short prefix for private socket directories."""

    safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    return safe[:32] or "target"


def _control_socket_root(runtime_dir: Path, target: str) -> Path:
    """Prefer common-dir storage, falling back to a short private temp root."""

    preferred = runtime_dir / "ssh"
    # OpenSSH appends its own random suffix while binding. Leave enough room
    # below common sockaddr_un limits instead of failing deep worktree paths.
    projected = preferred / f"{_safe_name(target)}-12345678/control.sock.1234567890123456"
    if len(os.fsencode(projected)) < 100:
        return preferred
    digest = hashlib.sha256(os.fsencode(runtime_dir)).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"git-deploy-{os.getuid()}" / digest


__all__ = [
    "OpenSSHEndpointKey",
    "OpenSSHMaster",
    "OpenSSHSFTPTransport",
    "PathProbeResult",
    "SSHConnectionPool",
]
