"""Install the built wheel in isolation and exercise its native CLI on each OS."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """Verify packaged entrypoints, completion resources and a Unicode dry-run."""

    root = Path(__file__).resolve().parents[1]
    wheel, = (root / "dist").glob("*.whl")
    (root / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheel-smoke-", dir=root / "tmp") as directory:
        base = Path(directory)
        environment = base / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        cli = scripts / ("git-deploy.exe" if os.name == "nt" else "git-deploy")
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], check=True)
        project = base / "项目 with spaces"
        project.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "Smoke"),
                     ("config", "user.email", "smoke@example.invalid")):
            subprocess.run(["git", *args], cwd=project, check=True)
        (project / "页面.txt").write_bytes("精确内容\n".encode("utf-8"))
        subprocess.run(["git", "add", "."], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "smoke"], cwd=project, check=True)
        (project / "deploy.toml").write_text(
            'default_target = "dev"\n[targets.dev]\nprotocol = "ftp"\n'
            'host = "example.invalid"\nusername = "deploy"\n'
            'password_env = "UNSET_SMOKE_PASSWORD"\nremote_root = "/app"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "GIT_DEPLOY_SKIP_COMPLETION_INSTALL": "1"}
        # Exercise the application's Windows pipe encoding, not Python UTF-8 mode.
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        for args in (("--version",), ("--help",), ("completion", "powershell"),
                     ("completion", "targets"), ("--dry-run", "--skip-build")):
            result = subprocess.run([str(cli), *args], cwd=project, env=env,
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            assert result.stdout, args
            if "--dry-run" in args:
                assert "页面.txt" in result.stdout
                assert not (project / ".git/git-deploy/dev.json").exists()
        print("Isolated wheel: version, help, PowerShell resource, targets and Unicode dry-run passed")


if __name__ == "__main__":
    main()
