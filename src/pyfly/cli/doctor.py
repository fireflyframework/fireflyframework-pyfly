# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""'pyfly doctor' — Diagnose PyFly environment and dependencies."""

from __future__ import annotations

import shutil
import sys

import click

from pyfly.cli.console import console

_MIN_PYTHON = (3, 12)

_REQUIRED_TOOLS = [
    ("git", "Version control"),
    ("uv", "Package manager"),
]

_OPTIONAL_TOOLS = [
    ("uvicorn", "ASGI server (pyfly run)"),
    ("alembic", "Database migrations (pyfly db)"),
    ("ruff", "Linter & formatter"),
    ("mypy", "Type checker"),
]


@click.command()
def doctor_command() -> None:
    """Check the PyFly development environment."""
    console.print("\n[pyfly]PyFly Doctor[/pyfly]\n")

    all_ok = True

    # Python version check
    py_version = sys.version_info
    py_ok = py_version >= _MIN_PYTHON
    if py_ok:
        console.print(f"  [success]\u2713[/success] Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        cur = f"{py_version.major}.{py_version.minor}.{py_version.micro}"
        req = f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}"
        console.print(f"  [error]\u2717[/error] Python {cur} (requires >={req})")
        all_ok = False

    # Virtual environment check
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        console.print("  [success]\u2713[/success] Virtual environment active")
    else:
        console.print("  [warning]![/warning] No virtual environment detected")

    # Required tools
    console.print("\n  [info]Required tools:[/info]")
    for tool, description in _REQUIRED_TOOLS:
        found = shutil.which(tool) is not None
        if found:
            console.print(f"    [success]\u2713[/success] {tool} \u2014 {description}")
        else:
            console.print(f"    [error]\u2717[/error] {tool} \u2014 {description} [dim](not found)[/dim]")
            all_ok = False

    # Optional tools
    console.print("\n  [info]Optional tools:[/info]")
    for tool, description in _OPTIONAL_TOOLS:
        found = shutil.which(tool) is not None
        if found:
            console.print(f"    [success]\u2713[/success] {tool} \u2014 {description}")
        else:
            console.print(f"    [dim]-[/dim] {tool} \u2014 {description} [dim](not found)[/dim]")

    # PyFly installation check
    console.print("\n  [info]PyFly packages:[/info]")
    try:
        from pyfly import __version__

        console.print(f"    [success]\u2713[/success] pyfly v{__version__}")
    except ImportError:
        console.print("    [error]\u2717[/error] pyfly [dim](not installed)[/dim]")
        all_ok = False

    # Summary
    console.print()
    if all_ok:
        console.print("  [success]All checks passed![/success]\n")
    else:
        console.print("  [warning]Some issues found. See above for details.[/warning]\n")
