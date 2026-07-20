"""Binary passive/active FTP adapter with idempotent file operations."""

from __future__ import annotations

import ftplib
import hashlib
import io
import os
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from git_deploy.config import TargetConfig
from git_deploy.errors import DeployError
from git_deploy.transports.base import (
    ProgressCallback,
    RemotePathType,
    Transport,
    is_stable_remote_component,
)

# Pure-FTPd (and similar) welcome banners embed session-volatile fields such as
# concurrent user counts and wall-clock time. Capability profiles bind to the
# banner hash; hashing those fields invalidates the profile every connect.
_VOLATILE_BANNER_BODY = re.compile(
    r"(?is)"
    r"(?:you are user number\s+\d+\s+of\s+\d+\s+allowed\.?|"
    r"local time is now\b.*)"
)


def normalize_ftp_server_banner(welcome: str) -> str:
    """Drop session-volatile FTP welcome lines before identity hashing.

    Args:
        welcome: Raw multi-line server greeting from ``FTP.getwelcome()``.

    Returns:
        Newline-joined stable banner lines. Empty input yields an empty string.
    """

    kept: list[str] = []
    for raw_line in welcome.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        body = line
        if len(line) >= 4 and line[:3].isdigit() and line[3] in "- ":
            body = line[4:].strip()
        if _VOLATILE_BANNER_BODY.fullmatch(body):
            continue
        kept.append(line)
    return "\n".join(kept)


class FTPPathProbeResult(Enum):
    """Classify an FTP directory probe without collapsing access errors."""

    EXISTS = "exists"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FTPDirectoryProbe:
    """Carry one directory probe result, cached names, and optional error detail."""

    result: FTPPathProbeResult
    entries: frozenset[str] = frozenset()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FTPRemoteEntry:
    """Describe one MLSD child with an explicit supported remote type."""

    path: str
    kind: RemotePathType
    size: int | None
    modify: str | None


class FTPTransport(Transport):
    """Upload and delete files below one configured FTP root."""

    def __init__(self, target: TargetConfig) -> None:
        """Create an unconnected FTP adapter.

        Args:
            target: Validated FTP target settings.
        """

        self.target = target
        self.ftp: ftplib.FTP | None = None
        self._directory_entries: dict[str, set[str]] = {}
        self._missing_directories: set[str] = set()
        self._typed_entries: dict[str, tuple[FTPRemoteEntry, ...]] = {}
        self._root_names: tuple[str, ...] | None = None
        self._root_types: dict[str, RemotePathType | None] = {}
        self._features: frozenset[str] | None = None
        self._require_utf8 = False
        self._required_server_banner_hash: str | None = None

    def connect(self) -> None:
        """Connect and authenticate with a password from the environment."""

        self._directory_entries.clear()
        self._missing_directories.clear()
        self._typed_entries.clear()
        self._root_names = None
        self._root_types.clear()
        self._features = None
        if not self.target.password_env:
            raise DeployError("FTP target is missing password_env")
        password = os.environ.get(self.target.password_env)
        if password is None:
            raise DeployError(f"required password environment variable is not set: {self.target.password_env}")
        ftp = ftplib.FTP()
        try:
            ftp.connect(self.target.host or "", self.target.port, timeout=self.target.timeout)
            ftp.login(self.target.username or "", password)
            ftp.set_pasv(self.target.passive)
            self.ftp = ftp
            if self._require_utf8:
                self._activate_required_utf8()
        except Exception as exc:
            try:
                ftp.close()
            except Exception:
                pass
            self.ftp = None
            raise DeployError(f"FTP connection failed for target {self.target.name}: {exc}") from exc

    def ensure_root(self) -> None:
        """Create the absolute configured FTP root recursively when missing."""

        self._mkdirs(self.target.remote_root.as_posix())

    def root_exists(self) -> bool:
        """Check the configured FTP root using the shared three-state probe."""

        probe = self._probe_directory(self.target.remote_root.as_posix())
        if probe.result is FTPPathProbeResult.EXISTS:
            return True
        if probe.result is FTPPathProbeResult.MISSING:
            return False
        raise DeployError(
            f"cannot inspect FTP root {self.target.remote_root}: {probe.error or 'unknown error'}"
        )

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        callback: ProgressCallback,
        *,
        executable: bool = False,
    ) -> None:
        """Stream one file in binary mode after creating its parent directories.

        Args:
            local_path: Frozen local file to stream.
            remote_path: Normalized relative target path.
            callback: Byte progress callback.
            executable: Unsupported POSIX executable-mode request.
        """

        if executable:
            raise DeployError(f"FTP cannot guarantee executable mode for {remote_path}")

        ftp = self._require_ftp()
        target = self._absolute(remote_path)
        self._mkdirs(PurePosixPath(target).parent.as_posix())
        total = local_path.stat().st_size
        transferred = 0

        def block_callback(block: bytes) -> None:
            """Adapt ftplib's block callback to the shared byte callback."""

            nonlocal transferred
            transferred += len(block)
            callback(transferred, total)

        try:
            with local_path.open("rb") as handle:
                # Directory preparation is setup latency, so activate transfer
                # timing only immediately before STOR starts sending bytes.
                callback(0, total)
                ftp.storbinary(f"STOR {target}", handle, blocksize=64 * 1024, callback=block_callback)
            self._cache_add(PurePosixPath(target).parent.as_posix(), PurePosixPath(target).name)
            self._typed_entries.clear()
        except (OSError, ftplib.Error) as exc:
            raise DeployError(f"FTP upload failed for {remote_path}: {exc}") from exc

    def delete(self, remote_path: str) -> None:
        """Delete one file after a language-independent parent listing probe.

        Args:
            remote_path: Normalized relative target path.

        Returns:
            ``None`` after confirmed absence or successful deletion.
        """

        ftp = self._require_ftp()
        target = self._absolute(remote_path)
        parent = PurePosixPath(target).parent.as_posix()
        name = PurePosixPath(target).name
        probe = self._probe_directory(parent)
        if probe.result is FTPPathProbeResult.MISSING:
            return
        if probe.result is FTPPathProbeResult.ERROR:
            raise DeployError(
                f"FTP existence probe failed for {remote_path}: {probe.error or 'unknown error'}"
            )
        if name not in probe.entries:
            return
        try:
            ftp.delete(target)
        except ftplib.error_perm as exc:
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc
        except ftplib.Error as exc:
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc
        self._cache_discard(parent, name)

    def server_banner_hash(self) -> str:
        """Return a non-secret SHA256 identity for the connected server banner.

        Returns:
            Lowercase SHA256 of the normalized welcome response. Session-volatile
            Pure-FTPd fields (user count, local time) are stripped so Hybrid
            Capability Profiles remain valid across reconnects.
        """

        welcome = normalize_ftp_server_banner(self._require_ftp().getwelcome() or "")
        return hashlib.sha256(welcome.encode("utf-8", errors="replace")).hexdigest()

    def features(self) -> frozenset[str]:
        """Read and cache normalized FEAT capability names for this connection.

        Returns:
            Uppercase feature tokens, with MLST also implying MLSD availability.
        """

        if self._features is not None:
            return self._features
        try:
            response = self._require_ftp().sendcmd("FEAT")
        except ftplib.Error as exc:
            raise DeployError(f"FTP FEAT failed: {exc}") from exc
        features: set[str] = set()
        for raw_line in response.splitlines():
            line = raw_line.strip()
            if not line or line[:3].isdigit():
                continue
            token = line.split(None, 1)[0].rstrip(";").upper()
            if token:
                features.add(token)
        if "MLST" in features:
            features.add("MLSD")
        self._features = frozenset(features)
        return self._features

    def enable_utf8(self) -> None:
        """Require UTF-8 for this and every later connection of the transport.

        Returns:
            ``None`` after FEAT proves UTF-8 and the session encoding is set.
            Servers that advertise UTF8 but reject ``OPTS UTF8 ON`` (common on
            Pure-FTPd) are treated as always-on UTF-8; path semantics are still
            proven by the Hybrid capability probe.
        """

        self._activate_required_utf8()
        self._require_utf8 = True
        if self._required_server_banner_hash is None:
            self._required_server_banner_hash = self.server_banner_hash()

    def _activate_required_utf8(self) -> None:
        """Validate server identity and activate UTF-8 on the current session.

        Returns:
            ``None`` after FEAT UTF8 is present and the client encoding is utf-8.
            ``OPTS UTF8 ON`` is attempted when supported; permanent 5xx rejection
            is accepted for always-on servers such as Pure-FTPd.
        """

        actual_banner_hash = self.server_banner_hash()
        if (
            self._required_server_banner_hash is not None
            and actual_banner_hash != self._required_server_banner_hash
        ):
            raise DeployError(
                "FTP server banner changed while reconnecting; aborting before remote work"
            )
        if "UTF8" not in self.features():
            raise DeployError("FTP server does not advertise mandatory UTF8 support")
        ftp = self._require_ftp()
        try:
            response = ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.error_perm:
            # Pure-FTPd advertises FEAT UTF8 but returns 504 Unknown command for
            # OPTS; UTF-8 is always active when advertised. Temporary/network
            # errors still fail closed via other ftplib.Error subclasses.
            pass
        except ftplib.Error as exc:
            raise DeployError(f"FTP OPTS UTF8 ON failed: {exc}") from exc
        else:
            if response.startswith("2"):
                pass
            elif response[:1] == "5":
                # Permanent rejection without raising (rare, but same always-on case).
                pass
            else:
                raise DeployError(f"FTP OPTS UTF8 ON was not accepted: {response}")
        ftp.encoding = "utf-8"

    def lstat(
        self,
        remote_path: str,
        *,
        allow_case_collisions: bool = False,
    ) -> RemotePathType:
        """Classify one FTP path only from MLSD facts without LIST/NLST guessing.

        Args:
            remote_path: Safe relative path below the configured root.
            allow_case_collisions: Permit colliding siblings only during the
                explicit path-semantics capability probe.

        Returns:
            Explicit file, directory, or confirmed missing type.
        """

        path = PurePosixPath(remote_path)
        if path.is_absolute() or ".." in path.parts:
            raise DeployError(f"unsafe FTP relative path: {remote_path!r}")
        if path.as_posix() in {"", "."}:
            return RemotePathType.DIRECTORY if self.root_exists() else RemotePathType.MISSING
        current = "."
        for index, component in enumerate(path.parts):
            if component == ".":
                continue
            if current == ".":
                # Root may contain unrelated types and name collisions owned by
                # other systems. Planned aliases are gated before managed work.
                self.list_root_names()
                if component not in self._root_types:
                    return RemotePathType.MISSING
                kind = self._root_types[component]
                if kind is None:
                    raise DeployError(
                        f"FTP MLSD returned an unsupported type for {component!r}"
                    )
            else:
                entries = {
                    item.path: item
                    for item in self.list_directory_typed(
                        current,
                        allow_case_collisions=allow_case_collisions,
                    )
                }
                entry = entries.get(component)
                if entry is None:
                    return RemotePathType.MISSING
                kind = entry.kind
            if index < len(path.parts) - 1 and kind is not RemotePathType.DIRECTORY:
                raise DeployError(f"FTP path parent is not a directory: {remote_path}")
            current = component if current == "." else f"{current}/{component}"
        return kind

    def list_directory_typed(
        self,
        remote_path: str,
        *,
        allow_case_collisions: bool = False,
    ) -> tuple[FTPRemoteEntry, ...]:
        """Return one MLSD directory listing with strict names and types.

        Args:
            remote_path: Safe relative directory below the configured root.
            allow_case_collisions: Permit colliding names only for the explicit
                filesystem-semantics capability probe.

        Returns:
            Stable direct children; cdir/pdir pseudo entries are omitted.
        """

        absolute = self._absolute(remote_path)
        cached = self._typed_entries.get(absolute)
        if cached is not None:
            if not allow_case_collisions:
                _reject_ftp_name_collisions(cached, remote_path)
            return cached
        try:
            raw_entries = tuple(self._require_ftp().mlsd(absolute))
        except ftplib.Error as exc:
            raise DeployError(f"FTP MLSD failed for {remote_path}: {exc}") from exc
        entries: list[FTPRemoteEntry] = []
        for name, raw_facts in raw_entries:
            facts = {key.casefold(): value for key, value in raw_facts.items()}
            kind_text = facts.get("type", "").casefold()
            if kind_text in {"cdir", "pdir"}:
                continue
            if not is_stable_remote_component(name):
                raise DeployError(f"FTP MLSD returned an unsafe name: {name!r}")
            if kind_text == "file":
                kind = RemotePathType.FILE
            elif kind_text == "dir":
                kind = RemotePathType.DIRECTORY
            else:
                raise DeployError(
                    f"FTP MLSD returned unsupported type {kind_text or '<missing>'!r} "
                    f"for {name!r}"
                )
            size_text = facts.get("size")
            try:
                size = int(size_text) if size_text is not None else None
            except ValueError as exc:
                raise DeployError(f"FTP MLSD returned invalid size for {name!r}") from exc
            if size is not None and size < 0:
                raise DeployError(f"FTP MLSD returned invalid size for {name!r}")
            entries.append(FTPRemoteEntry(name, kind, size, facts.get("modify")))
        result = tuple(sorted(entries, key=lambda item: item.path))
        if not allow_case_collisions:
            _reject_ftp_name_collisions(result, remote_path)
        self._typed_entries[absolute] = result
        return result

    def list_root_names(self) -> tuple[str, ...]:
        """Return stable root child names without interpreting unrelated types.

        Returns:
            Sorted exact MLSD names, including types not managed by git-deploy.
        """

        if self._root_names is not None:
            return self._root_names
        try:
            raw_entries = tuple(self._require_ftp().mlsd(self._absolute(".")))
        except ftplib.Error as exc:
            raise DeployError(f"FTP MLSD failed for .: {exc}") from exc
        names: list[str] = []
        types: dict[str, RemotePathType | None] = {}
        for name, raw_facts in raw_entries:
            facts = {key.casefold(): value for key, value in raw_facts.items()}
            kind_text = facts.get("type", "").casefold()
            if kind_text in {"cdir", "pdir"}:
                continue
            if not is_stable_remote_component(name):
                raise DeployError(f"FTP MLSD returned an unsafe name: {name!r}")
            names.append(name)
            if kind_text == "file":
                types[name] = RemotePathType.FILE
            elif kind_text == "dir":
                types[name] = RemotePathType.DIRECTORY
            else:
                types[name] = None
        self._root_names = tuple(sorted(names))
        self._root_types = types
        return self._root_names

    def refresh_remote_metadata(self) -> None:
        """Discard every cached remote listing before a freshness gate.

        Returns:
            ``None`` after typed MLSD, NLST, and confirmed-missing caches clear.
        """

        self._clear_remote_caches()

    def read_file(
        self,
        remote_path: str,
        *,
        max_bytes: int,
        allow_case_collisions: bool = False,
    ) -> bytes:
        """Read a bounded FTP regular file in binary mode.

        Args:
            remote_path: Safe relative file below the configured root.
            max_bytes: Strict maximum accepted byte count.
            allow_case_collisions: Permit the two deliberate case variants used
                by the explicit path-semantics capability probe.

        Returns:
            Exact bytes when the MLSD type is File and the bound is respected.
        """

        if max_bytes < 0:
            raise DeployError("FTP read max_bytes must be non-negative")
        kind = self.lstat(
            remote_path,
            allow_case_collisions=allow_case_collisions,
        )
        if kind is RemotePathType.MISSING:
            raise DeployError(f"FTP file is missing: {remote_path}")
        if kind is not RemotePathType.FILE:
            raise DeployError(f"FTP path is not a regular file: {remote_path}")
        output = io.BytesIO()

        def receive(block: bytes) -> None:
            """Append one RETR block while enforcing the caller's hard limit."""

            if output.tell() + len(block) > max_bytes:
                raise DeployError(f"FTP file exceeds {max_bytes} byte limit: {remote_path}")
            output.write(block)

        try:
            self._require_ftp().retrbinary(f"RETR {self._absolute(remote_path)}", receive)
        except DeployError:
            raise
        except ftplib.Error as exc:
            raise DeployError(f"FTP RETR failed for {remote_path}: {exc}") from exc
        return output.getvalue()

    def write_bytes(self, remote_path: str, data: bytes) -> None:
        """Upload complete in-memory bytes in binary mode for internal metadata.

        Args:
            remote_path: Safe relative destination below the configured root.
            data: Complete payload.

        Returns:
            ``None`` after STOR succeeds.
        """

        absolute = self._absolute(remote_path)
        self._mkdirs(PurePosixPath(absolute).parent.as_posix())
        try:
            self._require_ftp().storbinary("STOR " + absolute, io.BytesIO(data), blocksize=64 * 1024)
        except ftplib.Error as exc:
            raise DeployError(f"FTP STOR failed for {remote_path}: {exc}") from exc
        self._clear_remote_caches()

    def rename_replace(self, source: str, destination: str) -> None:
        """Publish one staged FTP file using the probed replace-rename contract.

        Args:
            source: Existing staged relative file.
            destination: Final relative file, which may already exist.

        Returns:
            ``None`` after RNFR/RNTO succeeds.
        """

        try:
            self._require_ftp().rename(self._absolute(source), self._absolute(destination))
        except ftplib.Error as exc:
            raise DeployError(f"FTP rename replace failed for {source} -> {destination}: {exc}") from exc
        self._clear_remote_caches()

    def delete_typed(
        self,
        remote_path: str,
        *,
        allow_case_collisions: bool = False,
    ) -> None:
        """Idempotently delete a path proven by MLSD to be a regular file."""

        kind = self.lstat(
            remote_path,
            allow_case_collisions=allow_case_collisions,
        )
        if kind is RemotePathType.MISSING:
            return
        if kind is not RemotePathType.FILE:
            raise DeployError(f"FTP delete target is not a regular file: {remote_path}")
        try:
            self._require_ftp().delete(self._absolute(remote_path))
        except ftplib.Error as exc:
            raise DeployError(f"FTP delete failed for {remote_path}: {exc}") from exc
        self._clear_remote_caches()

    def make_directory(self, remote_path: str, *, mode: int = 0o755) -> None:
        """Create one FTP directory tree; POSIX mode is intentionally ignored."""

        del mode
        self._mkdirs(self._absolute(remote_path))
        self._clear_remote_caches()

    def remove_directory(
        self,
        remote_path: str,
        *,
        allow_name_collisions: bool = False,
    ) -> None:
        """Idempotently remove one MLSD-proven empty FTP directory."""

        kind = self.lstat(remote_path, allow_case_collisions=allow_name_collisions)
        if kind is RemotePathType.MISSING:
            return
        if kind is not RemotePathType.DIRECTORY:
            raise DeployError(f"FTP RMD target is not a directory: {remote_path}")
        if self.list_directory_typed(
            remote_path,
            allow_case_collisions=allow_name_collisions,
        ):
            raise DeployError(f"FTP RMD target is not empty: {remote_path}")
        try:
            self._require_ftp().rmd(self._absolute(remote_path))
        except ftplib.Error as exc:
            raise DeployError(f"FTP RMD failed for {remote_path}: {exc}") from exc
        self._clear_remote_caches()

    def remove_tree(
        self,
        remote_path: str,
        *,
        allow_name_collisions: bool = False,
    ) -> None:
        """Remove one internal FTP tree using only typed MLSD traversal."""

        kind = self.lstat(remote_path, allow_case_collisions=allow_name_collisions)
        if kind is RemotePathType.MISSING:
            return
        if kind is RemotePathType.FILE:
            self.delete_typed(remote_path, allow_case_collisions=allow_name_collisions)
            return
        if kind is not RemotePathType.DIRECTORY:
            raise DeployError(f"FTP tree has unsupported type: {remote_path}")
        for entry in self.list_directory_typed(
            remote_path,
            allow_case_collisions=allow_name_collisions,
        ):
            child = f"{remote_path}/{entry.path}"
            self.remove_tree(child, allow_name_collisions=allow_name_collisions)
        self.remove_directory(remote_path, allow_name_collisions=allow_name_collisions)

    def close(self) -> None:
        """Quit cleanly when possible, otherwise close the socket."""

        self._directory_entries.clear()
        self._missing_directories.clear()
        self._typed_entries.clear()
        self._root_names = None
        self._root_types.clear()
        self._features = None
        if self.ftp is None:
            return
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass
        finally:
            self.ftp = None

    def _mkdirs(self, absolute: str) -> None:
        """Create missing FTP directories while preserving existing ones."""

        ftp = self._require_ftp()
        current = "/"
        for component in PurePosixPath(absolute).parts[1:]:
            current = (PurePosixPath(current) / component).as_posix()
            try:
                ftp.mkd(current)
                parent = PurePosixPath(current).parent.as_posix()
                self._cache_add(parent, PurePosixPath(current).name)
                self._directory_entries[current] = set()
                self._missing_directories.discard(current)
            except ftplib.error_perm as exc:
                if not str(exc).startswith("550"):
                    raise DeployError(f"cannot create FTP directory {current}: {exc}") from exc
                try:
                    original = ftp.pwd()
                    ftp.cwd(current)
                    ftp.cwd(original)
                    self._missing_directories.discard(current)
                except ftplib.Error as inspect_exc:
                    raise DeployError(f"FTP directory is unavailable {current}: {inspect_exc}") from inspect_exc

    def _probe_directory(self, absolute: str) -> FTPDirectoryProbe:
        """List one directory once and distinguish missing parents from access errors.

        Args:
            absolute: Absolute POSIX directory path below or containing the target root.

        Returns:
            Explicit probe state with normalized child names when the directory exists.
        """

        cached = self._directory_entries.get(absolute)
        if cached is not None:
            return FTPDirectoryProbe(FTPPathProbeResult.EXISTS, frozenset(cached))
        if absolute in self._missing_directories:
            return FTPDirectoryProbe(FTPPathProbeResult.MISSING)
        ftp = self._require_ftp()
        try:
            entries = {
                PurePosixPath(entry.rstrip("/")).name
                for entry in ftp.nlst(absolute)
                if entry.rstrip("/")
            }
        except ftplib.error_perm as exc:
            recovered = self._recover_failed_listing(absolute, exc)
            if recovered.result is FTPPathProbeResult.MISSING:
                self._missing_directories.add(absolute)
            return recovered
        except ftplib.Error as exc:
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=str(exc))
        self._directory_entries[absolute] = entries
        self._missing_directories.discard(absolute)
        return FTPDirectoryProbe(FTPPathProbeResult.EXISTS, frozenset(entries))

    def _recover_failed_listing(
        self,
        absolute: str,
        error: ftplib.error_perm,
    ) -> FTPDirectoryProbe:
        """Resolve a failed NLST through CWD and the nearest listable ancestor.

        Args:
            absolute: Directory whose listing failed.
            error: Permanent FTP response returned by NLST.

        Returns:
            Missing for an absent parent, Exists for an empty accessible directory,
            or Error when permissions and absence cannot be separated safely.
        """

        detail = str(error)
        if _looks_like_access_denied(detail):
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=detail)
        ftp = self._require_ftp()
        try:
            original = ftp.pwd()
            ftp.cwd(absolute)
            ftp.cwd(original)
        except ftplib.error_perm as cwd_error:
            cwd_detail = str(cwd_error)
            if _looks_like_access_denied(cwd_detail) or absolute == "/":
                return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=cwd_detail)
            parent = PurePosixPath(absolute).parent.as_posix()
            ancestor = self._probe_directory(parent)
            if ancestor.result is not FTPPathProbeResult.EXISTS:
                return ancestor
            name = PurePosixPath(absolute).name
            if name not in ancestor.entries:
                return FTPDirectoryProbe(FTPPathProbeResult.MISSING)
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=cwd_detail)
        except ftplib.Error as cwd_error:
            return FTPDirectoryProbe(FTPPathProbeResult.ERROR, error=str(cwd_error))
        # Several servers report 550 for NLST on an empty directory. Successful
        # CWD proves the parent exists, so the requested child is already absent.
        self._directory_entries[absolute] = set()
        self._missing_directories.discard(absolute)
        return FTPDirectoryProbe(FTPPathProbeResult.EXISTS)

    def _cache_add(self, parent: str, name: str) -> None:
        """Add a known child only when its parent already has a complete listing."""

        entries = self._directory_entries.get(parent)
        if entries is not None:
            entries.add(name)

    def _cache_discard(self, parent: str, name: str) -> None:
        """Remove a successfully deleted child from a cached complete listing."""

        entries = self._directory_entries.get(parent)
        if entries is not None:
            entries.discard(name)

    def _clear_remote_caches(self) -> None:
        """Discard listings after any mutation so retries observe server facts."""

        self._directory_entries.clear()
        self._missing_directories.clear()
        self._typed_entries.clear()
        self._root_names = None
        self._root_types.clear()

    def _absolute(self, relative: str) -> str:
        """Join a normalized relative path below the configured root."""

        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts:
            raise DeployError(f"unsafe FTP relative path: {relative!r}")
        return (self.target.remote_root / path).as_posix()

    def _require_ftp(self) -> ftplib.FTP:
        """Return the active FTP session or fail clearly."""

        if self.ftp is None:
            raise DeployError("FTP transport is not connected")
        return self.ftp


def _looks_like_access_denied(detail: str) -> bool:
    """Recognize explicit access failures while keeping generic 550 replies ambiguous."""

    normalized = detail.casefold()
    return any(
        marker in normalized
        for marker in ("permission", "access denied", "not allowed", "not permitted", "forbidden")
    )


def _reject_ftp_name_collisions(
    entries: tuple[FTPRemoteEntry, ...],
    remote_path: str,
) -> None:
    """Reject duplicate or non-portable sibling names returned by MLSD.

    Args:
        entries: Parsed direct MLSD children.
        remote_path: Relative parent rendered in the diagnostic.

    Returns:
        ``None`` when every child is unique after NFC normalization and case folding.
    """

    seen: dict[str, str] = {}
    for entry in entries:
        key = unicodedata.normalize("NFC", entry.path).casefold()
        previous = seen.get(key)
        if previous is not None:
            raise DeployError(
                "FTP MLSD returned colliding sibling names below "
                f"{remote_path!r}: {previous!r}, {entry.path!r}"
            )
        seen[key] = entry.path


__all__ = ["FTPDirectoryProbe", "FTPPathProbeResult", "FTPRemoteEntry", "FTPTransport"]
