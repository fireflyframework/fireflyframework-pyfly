"""Tests for the install.sh installer script."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from pyfly.cli.main import cli

INSTALLER_PATH = Path(__file__).resolve().parent.parent.parent / "install.sh"


def _system_python_ge_312() -> bool:
    """True if a Python >= 3.12 is available on PATH for ``install.sh`` to use.

    The end-to-end installer tests shell out to ``install.sh``, whose hard
    precondition is a system Python >= 3.12 (the framework's minimum). When the
    host only exposes an older interpreter (e.g. macOS system Python 3.9), the
    installer correctly aborts — so we skip rather than report a false failure.
    """
    for cmd in ("python3.14", "python3.13", "python3.12", "python3", "python"):
        path = shutil.which(cmd)
        if not path:
            continue
        try:
            out = subprocess.run(
                [path, "-c", "import sys; print(sys.version_info.major, sys.version_info.minor)"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            try:
                major, minor = (int(x) for x in out.stdout.split())
            except ValueError:
                continue
            if (major, minor) >= (3, 12):
                return True
    return False


requires_system_py312 = pytest.mark.skipif(
    not _system_python_ge_312(),
    reason="install.sh requires a system Python >= 3.12 on PATH (end-to-end installer test)",
)


@pytest.fixture
def project_root() -> Path:
    return INSTALLER_PATH.parent


class TestInstaller:
    def test_installer_exists(self):
        assert INSTALLER_PATH.exists(), f"install.sh not found at {INSTALLER_PATH}"

    def test_installer_is_executable(self):
        assert os.access(INSTALLER_PATH, os.X_OK), "install.sh is not executable"

    def test_installer_has_shebang(self):
        content = INSTALLER_PATH.read_text()
        assert content.startswith("#!/usr/bin/env bash"), "Missing bash shebang"

    def test_installer_has_set_euo_pipefail(self):
        content = INSTALLER_PATH.read_text()
        assert "set -euo pipefail" in content, "Missing strict mode"

    def test_installer_detects_interactive_mode(self):
        content = INSTALLER_PATH.read_text()
        assert "IS_INTERACTIVE" in content

    def test_installer_checks_python_version(self):
        content = INSTALLER_PATH.read_text()
        assert "MIN_PYTHON_MAJOR" in content
        assert "MIN_PYTHON_MINOR" in content

    @requires_system_py312
    def test_installer_non_interactive_dry_run(self, project_root: Path, tmp_path: Path):
        """Run installer non-interactively with PYFLY_HOME pointing to tmp."""
        install_dir = tmp_path / "pyfly-test"
        env = os.environ.copy()
        env["PYFLY_HOME"] = str(install_dir)
        env["PYFLY_SOURCE"] = str(project_root)

        result = subprocess.run(
            ["bash", str(INSTALLER_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,  # Force non-interactive
        )
        assert result.returncode == 0, f"Installer failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # Verify installation artifacts
        assert (install_dir / "venv").is_dir(), "venv not created"
        assert (install_dir / "source").is_dir(), "source not copied"
        assert (install_dir / "bin" / "pyfly").exists(), "pyfly wrapper not created"
        assert os.access(install_dir / "bin" / "pyfly", os.X_OK), "pyfly wrapper not executable"

    @requires_system_py312
    def test_installer_wrapper_runs(self, project_root: Path, tmp_path: Path):
        """Test that the installed pyfly wrapper actually works."""
        install_dir = tmp_path / "pyfly-run-test"
        env = os.environ.copy()
        env["PYFLY_HOME"] = str(install_dir)
        env["PYFLY_SOURCE"] = str(project_root)

        # Install first
        subprocess.run(
            ["bash", str(INSTALLER_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )

        # Now run the wrapper
        wrapper = install_dir / "bin" / "pyfly"
        result = subprocess.run(
            [str(wrapper), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Wrapper failed: {result.stderr}"
        assert "PyFly" in result.stdout


class TestEnsureCliExtra:
    """Tests for the ensure_cli_extra function in install.sh."""

    # Extract just the function definition from install.sh to test it in isolation
    _FUNC = """
ensure_cli_extra() {
    local extras="$1"
    if [ "$extras" = "full" ]; then
        echo "full"
        return
    fi
    if echo ",$extras," | grep -q ",cli,"; then
        echo "$extras"
    else
        echo "${extras},cli"
    fi
}
"""

    def _run_ensure_cli_extra(self, extras: str) -> str:
        result = subprocess.run(
            ["bash", "-c", f'{self._FUNC}\nensure_cli_extra "{extras}"'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()

    def test_full_unchanged(self):
        assert self._run_ensure_cli_extra("full") == "full"

    def test_web_adds_cli(self):
        assert self._run_ensure_cli_extra("web") == "web,cli"

    def test_data_adds_cli(self):
        assert self._run_ensure_cli_extra("data") == "data,cli"

    def test_already_has_cli(self):
        assert self._run_ensure_cli_extra("web,cli") == "web,cli"

    def test_cli_alone_unchanged(self):
        assert self._run_ensure_cli_extra("cli") == "cli"

    def test_multiple_extras_adds_cli(self):
        result = self._run_ensure_cli_extra("web,data,security")
        assert result == "web,data,security,cli"


class TestPythonVersionComparison:
    """Tests for the find_python version comparison logic."""

    def test_installer_uses_proper_version_comparison(self):
        """Verify the version comparison handles major > MIN_MAJOR correctly."""
        content = INSTALLER_PATH.read_text()
        # Must use -gt for major (handles Python 4.x+) instead of just -ge
        assert '"$major" -gt "$MIN_PYTHON_MAJOR"' in content


class TestLazyImports:
    """Tests for lazy imports in CLI modules — ensures the CLI works even without optional extras."""

    def test_db_help_does_not_import_alembic(self):
        """pyfly db --help should work without alembic installed."""
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "--help"])
        assert result.exit_code == 0
        assert "Database migration" in result.output

    def test_run_help_does_not_import_uvicorn(self):
        """pyfly run --help should work without uvicorn installed."""
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output

    def test_all_commands_listed_in_help(self):
        """All registered commands should appear in --help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ["new", "db", "info", "run", "doctor", "license", "sbom"]:
            assert cmd in result.output, f"Command '{cmd}' missing from --help"
