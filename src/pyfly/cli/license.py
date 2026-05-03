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
"""'pyfly license' — Display the project license."""

from __future__ import annotations

from importlib import resources

import click

from pyfly.cli.console import console


@click.command()
def license_command() -> None:
    """Display the Apache 2.0 license for PyFly."""
    console.print("\n[pyfly]PyFly Framework[/pyfly] [dim]— License[/dim]\n")
    console.print("[bold]Apache License, Version 2.0[/bold]")
    console.print("[dim]Copyright 2026 Firefly Software Foundation.[/dim]\n")

    # Try to read the full LICENSE file from the package or project root
    license_text = _load_license()
    if license_text:
        console.print(license_text)
    else:
        console.print(
            "Licensed under the Apache License, Version 2.0.\n"
            "You may obtain a copy of the License at:\n\n"
            "    [link]http://www.apache.org/licenses/LICENSE-2.0[/link]\n"
        )


def _load_license() -> str | None:
    """Attempt to read LICENSE from package resources or filesystem."""
    # Try importlib.resources (works in installed packages)
    try:
        ref = resources.files("pyfly").joinpath("../../LICENSE")
        text = ref.read_text(encoding="utf-8")
        if text.strip():
            return text
    except Exception:
        pass

    # Try reading from common filesystem locations
    from pathlib import Path

    for candidate in [
        Path(__file__).resolve().parent.parent.parent.parent / "LICENSE",
        Path.cwd() / "LICENSE",
    ]:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            pass

    return None
