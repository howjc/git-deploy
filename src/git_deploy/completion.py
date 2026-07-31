"""Shell Tab completion helpers for the flat git-deploy CLI.

Target listing stays read-only and never opens remotes or loads secrets.
``install_shell_completion`` may write user-local completion files and RC
snippets; it still never contacts remotes. Ordinary CLI entry never rewrites
shell RC files — users opt in with ``git-deploy completion install``.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import stat
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator, Literal

from git_deploy import __version__
from git_deploy.config import CLI_FIXED_ACTIONS, is_valid_target_name
from git_deploy.errors import ConfigError

# Fixed first-position words (not deploy targets); same set as config reserved names.
FIXED_ACTIONS: tuple[str, ...] = CLI_FIXED_ACTIONS

# Second position after ``completion``.
COMPLETION_SHELLS: tuple[str, ...] = ("bash", "zsh", "targets", "install")

SupportedShell = Literal["bash", "zsh"]

# Public CLI option strings for static scripts and tests (long form only).
CLI_OPTION_FLAGS: tuple[str, ...] = (
    "--dry-run",
    "--remote-plan",
    "--recover",
    "--skip-build",
    "--full",
    "--yes",
    "--config",
    "--workspace",
    "--verbose",
    "--create-root",
    "--no-create-root",
    "--force",
    "--probe-ftp-hybrid",
    "--version",
    "--help",
)

_RC_BEGIN = "# >>> git-deploy shell completion >>>"
_RC_END = "# <<< git-deploy shell completion <<<"
_STATE_RELATIVE = Path(".config/git-deploy/completion-install.version")


def list_target_names(
    *,
    config: Path | None = None,
    workspace: Path | None = None,
    cwd: Path | None = None,
) -> tuple[str, ...]:
    """Return target names for Tab completion without full config validation.

    Args:
        config: Explicit project ``deploy.toml`` path, if the user already typed
            ``--config``.
        workspace: Explicit ``deploy.workspace.toml`` path, if the user already
            typed ``--workspace``.
        cwd: Working directory used for local discovery; defaults to ``Path.cwd()``.

    Returns:
        Sorted unique target names, or an empty tuple when discovery is ambiguous
        or no readable configuration is found. Never raises.
    """

    try:
        return _list_target_names_impl(config=config, workspace=workspace, cwd=cwd)
    except Exception:
        return ()


def list_action_completions(
    *,
    config: Path | None = None,
    workspace: Path | None = None,
    cwd: Path | None = None,
    prefix: str = "",
) -> list[str]:
    """Return fixed actions plus discovered targets matching ``prefix``.

    Args:
        config: Optional explicit project config path.
        workspace: Optional explicit workspace path.
        cwd: Working directory for discovery.
        prefix: Current token prefix from the shell.

    Returns:
        Completions suitable for argcomplete or static scripts.
    """

    names = (*FIXED_ACTIONS, *list_target_names(config=config, workspace=workspace, cwd=cwd))
    return [name for name in names if name.startswith(prefix)]


def list_extra_completions(
    action: str | None,
    *,
    config: Path | None = None,
    workspace: Path | None = None,
    cwd: Path | None = None,
    prefix: str = "",
) -> list[str]:
    """Return second-position completions for doctor/bootstrap/completion.

    Args:
        action: First positional token already parsed, if any.
        config: Optional explicit project config path.
        workspace: Optional explicit workspace path.
        cwd: Working directory for discovery.
        prefix: Current token prefix from the shell.

    Returns:
        Shell or target names matching ``prefix``.
    """

    if action == "completion":
        return [name for name in COMPLETION_SHELLS if name.startswith(prefix)]
    targets = list_target_names(config=config, workspace=workspace, cwd=cwd)
    return [name for name in targets if name.startswith(prefix)]


def load_completion_script(shell: str) -> str:
    """Load a packaged static completion script.

    Args:
        shell: ``bash`` or ``zsh``.

    Returns:
        Script text ending with a newline.

    Raises:
        ValueError: When ``shell`` is not a packaged static script.
        FileNotFoundError: When the packaged resource is missing.
    """

    if shell not in ("bash", "zsh"):
        raise ValueError(f"unsupported completion shell: {shell!r}")
    resource = files("git_deploy.completions").joinpath(f"git-deploy.{shell}")
    text = resource.read_text(encoding="utf-8")
    return text if text.endswith("\n") else f"{text}\n"


@dataclass(frozen=True, slots=True)
class CompletionInstallResult:
    """Describe one shell completion install attempt."""

    shell: SupportedShell
    script_path: Path
    rc_path: Path
    script_written: bool
    rc_updated: bool
    already_current: bool


def detect_login_shell(shell: str | None = None) -> SupportedShell | None:
    """Map ``$SHELL`` (or an override) to a supported completion shell.

    Args:
        shell: Explicit shell path or name; defaults to ``os.environ['SHELL']``.

    Returns:
        ``bash`` / ``zsh``, or ``None`` when the shell is unsupported.
    """

    raw = (shell if shell is not None else os.environ.get("SHELL", "")).strip()
    if not raw:
        return None
    name = Path(raw).name.lower()
    if name in {"bash", "bash.exe"}:
        return "bash"
    if name in {"zsh", "zsh.exe"}:
        return "zsh"
    return None


def completion_script_path(shell: SupportedShell, *, home: Path | None = None) -> Path:
    """Return the user-local path where the static completion script is installed.

    Args:
        shell: Target shell.
        home: Home directory override (tests); defaults to ``Path.home()``.

    Returns:
        Absolute script path under XDG-style user data directories.
    """

    base = (home or Path.home()).expanduser().resolve()
    if shell == "bash":
        return base / ".local/share/bash-completion/completions/git-deploy"
    return base / ".local/share/zsh/site-functions/_git-deploy"


def completion_rc_path(shell: SupportedShell, *, home: Path | None = None) -> Path:
    """Return the user RC file updated for completion loading.

    Args:
        shell: Target shell.
        home: Home directory override (tests); defaults to ``Path.home()``.

    Returns:
        ``~/.bashrc`` or ``~/.zshrc``.
    """

    base = (home or Path.home()).expanduser().resolve()
    return base / (".bashrc" if shell == "bash" else ".zshrc")


def install_shell_completion(
    shell: str | None = None,
    *,
    force: bool = False,
    home: Path | None = None,
    update_rc: bool = True,
) -> list[CompletionInstallResult]:
    """Detect the user shell and write completion scripts plus optional RC snippets.

    Args:
        shell: Optional shell name/path (``bash``, ``zsh``, or a full ``$SHELL``
            path). When omitted, uses ``$SHELL``; if still unknown, installs for
            every supported RC file that already exists under ``home``.
        force: Rewrite script and RC markers even when content is current.
        home: Home directory override for tests.
        update_rc: When ``True`` (explicit ``completion install``), upsert the
            marked RC source block. When ``False``, only write user-local
            scripts and never touch ``.bashrc`` / ``.zshrc``.

    Returns:
        One result per shell that was installed or already current.

    Raises:
        ValueError: When an explicit shell is unsupported.
        OSError: When a required path cannot be written.
    """

    home_path = (home or Path.home()).expanduser().resolve()
    targets = _resolve_install_shells(shell, home=home_path)
    if not targets:
        raise ValueError(
            "cannot detect a supported shell; pass bash or zsh explicitly "
            "(supported: bash, zsh)"
        )
    results: list[CompletionInstallResult] = []
    with _install_lock(home_path):
        for item in targets:
            results.append(
                _install_one_shell(
                    item,
                    force=force,
                    home=home_path,
                    update_rc=update_rc,
                )
            )
        if update_rc:
            _write_install_state(home_path, __version__)
    return results


def ensure_shell_completion_installed(
    *,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> list[CompletionInstallResult] | None:
    """Best-effort script-only install/refresh; never rewrites shell RC files.

    Ordinary CLI entry may place packaged completion scripts under the user
    data directory when they are missing, or atomically refresh them when the
    installed body differs from the packaged script. Prints a one-line note
    only when a write happens. RC edits require explicit
    ``git-deploy completion install``.

    Args:
        home: Home directory override for tests.
        environ: Environment override for tests; defaults to ``os.environ``.

    Returns:
        Install results when scripts were written, or ``None`` when skipped.
    """

    env = environ if environ is not None else os.environ
    if env.get("GIT_DEPLOY_SKIP_COMPLETION_INSTALL"):
        return None
    if env.get("_ARGCOMPLETE"):
        return None
    home_path = (home or Path.home()).expanduser().resolve()
    try:
        shell = detect_login_shell(env.get("SHELL"))
        if shell is None:
            return None
        script = completion_script_path(shell, home=home_path)
        if script.is_file():
            packaged = load_completion_script(shell)
            try:
                if script.read_text(encoding="utf-8") == packaged:
                    return None
            except OSError:
                pass
            # Stale or unreadable install: refresh script only (no RC rewrite).
            results = install_shell_completion(
                shell,
                force=False,
                home=home_path,
                update_rc=False,
            )
            if any(item.script_written for item in results):
                print(
                    "note: shell completion script refreshed under the user data "
                    "directory (RC unchanged); enable Tab completion with: "
                    "git-deploy completion install",
                    file=sys.stderr,
                )
                return results
            return None
        results = install_shell_completion(
            shell,
            force=False,
            home=home_path,
            update_rc=False,
        )
        print(
            "note: shell completion scripts installed under the user data "
            "directory; enable Tab completion with: "
            "git-deploy completion install",
            file=sys.stderr,
        )
        return results
    except Exception:
        return None


def format_install_report(results: list[CompletionInstallResult]) -> str:
    """Render a short human-readable install summary.

    Args:
        results: Outcomes from :func:`install_shell_completion`.

    Returns:
        Multi-line text suitable for stdout.
    """

    if not results:
        return "No shell completion changes."
    lines: list[str] = []
    for item in results:
        if item.already_current and not item.script_written and not item.rc_updated:
            lines.append(
                f"{item.shell}: already installed ({item.script_path})"
            )
            continue
        parts: list[str] = []
        if item.script_written:
            parts.append(f"script {item.script_path}")
        if item.rc_updated:
            parts.append(f"rc {item.rc_path}")
        if not parts:
            parts.append(f"verified {item.script_path}")
        lines.append(f"{item.shell}: wrote " + ", ".join(parts))
    lines.append("Reload the shell or run: source <rc-file>  (then try: git-deploy <Tab>)")
    return "\n".join(lines)


def _resolve_install_shells(shell: str | None, *, home: Path) -> list[SupportedShell]:
    """Choose which shells receive completion files."""

    if shell is not None and shell.strip() and shell.strip() not in {"install", "auto"}:
        detected = detect_login_shell(shell)
        if detected is None:
            name = Path(shell.strip()).name.lower()
            if name not in {"bash", "zsh"}:
                raise ValueError(f"unsupported completion shell: {shell!r}")
            detected = "bash" if name == "bash" else "zsh"
        return [detected]
    detected = detect_login_shell()
    if detected is not None:
        return [detected]
    found: list[SupportedShell] = []
    if (home / ".bashrc").is_file() or (home / ".bash_profile").is_file():
        found.append("bash")
    if (home / ".zshrc").is_file() or (home / ".zprofile").is_file():
        found.append("zsh")
    return found


def _install_one_shell(
    shell: SupportedShell,
    *,
    force: bool,
    home: Path,
    update_rc: bool = True,
) -> CompletionInstallResult:
    """Write one shell's completion script and optionally the RC source block.

    Args:
        shell: Target shell.
        force: Rewrite even when content matches.
        home: Resolved home directory.
        update_rc: When ``False``, leave RC files untouched.

    Returns:
        Install outcome for this shell.
    """

    script_path = completion_script_path(shell, home=home)
    rc_path = completion_rc_path(shell, home=home)
    script_body = load_completion_script(shell)
    script_written = _write_text_if_changed(script_path, script_body, force=force)
    if not update_rc:
        already = not script_written and script_path.is_file()
        return CompletionInstallResult(
            shell,
            script_path,
            rc_path,
            script_written,
            False,
            already,
        )
    rc_block = _rc_snippet(shell, script_path)
    rc_updated = _upsert_rc_block(rc_path, rc_block, force=force)
    already_current = not script_written and not rc_updated
    return CompletionInstallResult(
        shell=shell,
        script_path=script_path,
        rc_path=rc_path,
        script_written=script_written,
        rc_updated=rc_updated,
        already_current=already_current,
    )


def _rc_snippet(shell: SupportedShell, script_path: Path) -> str:
    """Build the marked RC fragment that sources the installed script.

    Paths are shell-quoted so home directories containing spaces, quotes, or
    ``$`` do not expand when the RC is sourced.
    """

    path_text = shlex.quote(str(script_path))
    if shell == "bash":
        body = f"[[ -r {path_text} ]] && source {path_text}"
    else:
        # Keep zsh completion on fpath and source the install script once.
        site = shlex.quote(str(script_path.parent))
        body = "\n".join(
            (
                f"fpath=({site} $fpath)",
                f"[[ -r {path_text} ]] && source {path_text}",
            )
        )
    return f"{_RC_BEGIN}\n{body}\n{_RC_END}\n"


def _write_text_if_changed(path: Path, content: str, *, force: bool) -> bool:
    """Atomically write ``content`` when missing, forced, or different.

    Args:
        path: Destination file.
        content: Full UTF-8 text to persist.
        force: Rewrite even when the on-disk bytes already match.

    Returns:
        ``True`` when the file was written.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if not force and path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    _atomic_write_text(path, content)
    return True


def _upsert_rc_block(rc_path: Path, block: str, *, force: bool) -> bool:
    """Insert or replace the marked completion block in a shell RC file.

    Uses an atomic replace so a crash mid-write cannot truncate the RC. The
    previous file mode is preserved when the RC already exists.

    Args:
        rc_path: User RC path (created when missing).
        block: Full marked block including trailing newline.
        force: Replace an existing identical-looking block even when equal.

    Returns:
        ``True`` when the RC file changed.
    """

    existing = ""
    if rc_path.is_file():
        existing = rc_path.read_text(encoding="utf-8")
    if _RC_BEGIN in existing and _RC_END in existing:
        start = existing.find(_RC_BEGIN)
        end = existing.find(_RC_END, start)
        if start != -1 and end != -1:
            end += len(_RC_END)
            # Consume a single trailing newline after the end marker.
            if end < len(existing) and existing[end] == "\n":
                end += 1
            current = existing[start:end]
            if current == block and not force:
                return False
            updated = existing[:start] + block + existing[end:]
            _atomic_write_text(rc_path, updated)
            return True
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(rc_path, prefix + block)
    return True


def _write_install_state(home: Path, version: str) -> None:
    """Persist the package version that last ran an explicit RC install."""

    path = home / _STATE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, f"{version}\n")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text via temp file, fsync, and ``os.replace``.

    When ``path`` is a symlink, follow it and write the resolved destination so
    the symlink node is never replaced by a regular file. Dangling symlinks
    fail with a clear error instead of being silently unlinked.

    Args:
        path: Final destination path (may be a symlink to the real RC file).
        content: Full file body.

    Raises:
        ConfigError: When ``path`` is a dangling symlink or cannot be resolved.
        OSError: When the temporary write or replace fails.
    """

    destination = path
    if path.is_symlink():
        try:
            destination = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigError(
                f"RC path {path} is a dangling or unresolvable symlink; "
                "add the completion block manually or fix the link target"
            ) from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous_mode: int | None = None
    if destination.is_file():
        previous_mode = stat.S_IMODE(destination.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(tmp_path, previous_mode)
        else:
            os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, destination)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@contextmanager
def _install_lock(home: Path) -> Iterator[None]:
    """Serialize concurrent completion installs for one home directory.

    Args:
        home: Resolved user home used as the lock namespace.

    Yields:
        ``None`` while the exclusive lock is held.
    """

    lock_path = home / ".config/git-deploy/completion-install.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _list_target_names_impl(
    *,
    config: Path | None,
    workspace: Path | None,
    cwd: Path | None,
) -> tuple[str, ...]:
    """Resolve target names with the same discovery priority as the CLI."""

    base = (cwd or Path.cwd()).resolve()
    if config is not None and workspace is not None:
        return ()
    if config is not None:
        return _target_keys_from_project(Path(config).expanduser())
    if workspace is not None:
        return _target_keys_from_workspace(Path(workspace).expanduser())

    local_config = base / "deploy.toml"
    local_workspace = base / "deploy.workspace.toml"
    if local_config.is_file() and local_workspace.is_file():
        # Match CLI: ambiguous without --config/--workspace.
        return ()
    if local_workspace.is_file():
        return _target_keys_from_workspace(local_workspace)
    if local_config.is_file():
        return _target_keys_from_project(local_config)

    home_config = Path.home() / ".config/git-deploy/deploy.toml"
    if home_config.is_file():
        return _target_keys_from_project(home_config)
    return ()


def _target_keys_from_project(path: Path) -> tuple[str, ...]:
    """Read ``[targets.*]`` table keys from one project deploy.toml."""

    raw = _load_toml_dict(path)
    if raw is None:
        return ()
    targets = raw.get("targets")
    if not isinstance(targets, dict):
        return ()
    names = [
        str(key)
        for key in targets
        if isinstance(key, str) and is_valid_target_name(key)
    ]
    return tuple(sorted(set(names)))


def _target_keys_from_workspace(path: Path) -> tuple[str, ...]:
    """Union target keys from every repository deploy.toml in a workspace file."""

    path = path.expanduser().resolve()
    raw = _load_toml_dict(path)
    if raw is None:
        return ()
    names: set[str] = set()
    default = raw.get("default_target")
    if isinstance(default, str) and is_valid_target_name(default.strip()):
        names.add(default.strip())
    entries = raw.get("repositories")
    if not isinstance(entries, list):
        return tuple(sorted(names))
    root = path.parent
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel.strip():
            continue
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        project = root / relative / "deploy.toml"
        names.update(_target_keys_from_project(project))
    return tuple(sorted(names))


def _load_toml_dict(path: Path) -> dict[str, Any] | None:
    """Load a TOML file as a dict, or return ``None`` on any failure."""

    try:
        resolved = path.expanduser().resolve()
        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None
