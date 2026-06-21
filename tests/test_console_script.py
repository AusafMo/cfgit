from __future__ import annotations

from pathlib import Path
import subprocess
import sysconfig


def test_cfg_console_script_runs_outside_project_dir() -> None:
    script = Path(sysconfig.get_path("scripts")) / "cfg"
    assert script.exists()

    for cwd in (Path("/tmp"), Path("/")):
        for _ in range(2):
            result = subprocess.run(
                [str(script), "--help"],
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
            assert "usage: cfg" in result.stdout
