"""Trusted serial shell build behavior tests."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from git_deploy.builder import run_build
from git_deploy.config import BuildConfig
from git_deploy.errors import BuildError


def test_build_runs_steps_serially_in_project_root(tmp_path: Path) -> None:
    """Each successful step sees earlier filesystem effects in project order."""

    config = BuildConfig(("printf first > result", "printf second >> result"), timeout=5)

    run_build(config, tmp_path)

    assert (tmp_path / "result").read_text(encoding="utf-8") == "firstsecond"


def test_build_stops_at_first_failure(tmp_path: Path) -> None:
    """A nonzero step prevents every later build command from running."""

    config = BuildConfig(("false", "touch should-not-exist"), timeout=5)

    with pytest.raises(BuildError, match="exit code"):
        run_build(config, tmp_path)

    assert not (tmp_path / "should-not-exist").exists()


def test_build_timeout_is_reported(tmp_path: Path) -> None:
    """An aggregate timeout terminates and categorizes the active build step."""

    with pytest.raises(BuildError, match="timed out"):
        run_build(BuildConfig(("sleep 1",), timeout=0.01), tmp_path)


def test_build_timeout_terminates_shell_children(tmp_path: Path) -> None:
    """A timed-out shell cannot leave a child that mutates the project later."""

    with pytest.raises(BuildError, match="timed out"):
        run_build(BuildConfig(("sleep 0.2; touch orphan",), timeout=0.01), tmp_path)
    time.sleep(0.3)

    assert not (tmp_path / "orphan").exists()
