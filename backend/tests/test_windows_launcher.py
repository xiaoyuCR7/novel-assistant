"""Exercise the BAT with a harmless script, never launch author-data services."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows BAT launcher")
LAUNCHER = Path(__file__).resolve().parents[2] / "start.bat"


def run_launcher(tmp_path, script):
    assert LAUNCHER.is_file(), "Missing one-click start.bat"
    project = tmp_path / "小说 studio & (test)!"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(LAUNCHER, project / "start.bat")
    if script is not None:
        (project / "scripts/dev.ps1").write_text(script, encoding="utf-8-sig")
    # cmd /s /c consumes the outer quote pair; retain the inner pair for the path.
    command = f'"{os.environ["COMSPEC"]}" /d /s /c ""{project / "start.bat"}""'
    return subprocess.run(
        command,
        cwd=tmp_path,
        input=b"\r\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
    )


@pytest.mark.parametrize("exit_code", [0, 23])
def test_launcher_uses_own_directory_and_preserves_exit_status(tmp_path, exit_code):
    result = run_launcher(tmp_path, f"""
if ((Get-Location).Path -ne (Split-Path $PSScriptRoot -Parent)) {{ exit 91 }}
Write-Output 'LAUNCHER_SCRIPT_EXECUTED'
exit {exit_code}
""")
    assert result.returncode == exit_code, result.stdout
    assert b"LAUNCHER_SCRIPT_EXECUTED" in result.stdout
    if exit_code:
        assert b"Startup failed" in result.stdout


def test_launcher_reports_missing_script_without_starting_services(tmp_path):
    result = run_launcher(tmp_path, None)
    assert result.returncode != 0
    assert b"Startup failed" in result.stdout
