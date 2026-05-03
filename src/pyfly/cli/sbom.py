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
"""'pyfly sbom' — Display the Software Bill of Materials."""

from __future__ import annotations

from importlib.metadata import requires, version

import click
from rich.table import Table

from pyfly import __version__
from pyfly.cli.console import console


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def sbom_command(*, as_json: bool) -> None:
    """Display the Software Bill of Materials (SBOM) for PyFly."""
    deps = _collect_dependencies()

    if as_json:
        import json

        payload = {
            "name": "pyfly",
            "version": __version__,
            "license": "Apache-2.0",
            "dependencies": [{"name": name, "required": req, "installed": inst} for name, req, inst in deps],
        }
        console.print_json(json.dumps(payload))
        return

    console.print(f"\n[pyfly]PyFly Framework[/pyfly] [dim]v{__version__} — SBOM[/dim]\n")

    table = Table(title="Software Bill of Materials", border_style="dim")
    table.add_column("Package", style="info")
    table.add_column("Required")
    table.add_column("Installed")

    for name, req, inst in deps:
        installed_style = "[success]" + inst + "[/success]" if inst else "[warning]not installed[/warning]"
        table.add_row(name, req, installed_style)

    console.print(table)
    console.print(f"\n[dim]Total dependencies: {len(deps)}[/dim]\n")


def _collect_dependencies() -> list[tuple[str, str, str]]:
    """Collect PyFly's dependencies with their required and installed versions."""
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    reqs = requires("pyfly") or []
    for req_str in reqs:
        # Skip extras-conditional deps that contain "extra ==" markers
        # We want all deps regardless of which extra pulled them in
        name = _parse_package_name(req_str)
        version_spec = _parse_version_spec(req_str)

        if name in seen:
            continue
        seen.add(name)

        installed_version = _get_installed_version(name)
        result.append((name, version_spec, installed_version))

    result.sort(key=lambda x: x[0].lower())
    return result


def _parse_package_name(req: str) -> str:
    """Extract the package name from a requirement string like 'pydantic>=2.0; extra == \"web\"'."""
    # Strip extras markers
    name = req.split(";")[0].strip()
    # Strip version specifiers
    for op in [">=", "<=", "==", "!=", "~=", "<", ">"]:
        name = name.split(op)[0]
    # Strip extras brackets like [standard]
    name = name.split("[")[0]
    return name.strip()


def _parse_version_spec(req: str) -> str:
    """Extract the version specifier from a requirement string."""
    base = req.split(";")[0].strip()
    name = _parse_package_name(req)
    spec = base[len(name) :].strip()
    # Remove extras like [standard]
    if spec.startswith("["):
        bracket_end = spec.find("]")
        if bracket_end != -1:
            spec = spec[bracket_end + 1 :].strip()
    return spec if spec else "*"


def _get_installed_version(name: str) -> str:
    """Get the installed version of a package, or empty string if not installed."""
    try:
        return version(name)
    except Exception:
        return ""
