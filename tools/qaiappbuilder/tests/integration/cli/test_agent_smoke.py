from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _qai(*args: str, repo_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    extra = os.pathsep.join([str(_REPO_ROOT / "src"), str(_REPO_ROOT)])
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = extra + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = extra
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.cli",
            "--repo-root",
            str(repo_root),
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
        check=False,
        encoding="utf-8",
        timeout=90,
    )


@pytest.mark.integration
def test_agent_help_reachable(tmp_path: Path) -> None:
    result = _qai("agent", "run", "--help", repo_root=tmp_path)

    assert result.returncode == 0, (
        f"STDOUT: {result.stdout!r}\nSTDERR: {result.stderr!r}"
    )
    assert "--stdin" in result.stdout
    assert "--prompt-file" in result.stdout
    assert "--output-last-message" in result.stdout
    assert "--workspace" in result.stdout
    assert "--connect-mcp" in result.stdout
    assert "--client-turn-id" in result.stdout
    assert "--timeout-seconds" in result.stdout
    assert "--format" in result.stdout
