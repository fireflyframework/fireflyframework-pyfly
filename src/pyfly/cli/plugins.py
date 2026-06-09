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
"""CLI plugin discovery — third-party subcommands via the 'pyfly.cli_plugins' entry-point group."""

from __future__ import annotations

from typing import Any

import click

from pyfly.cli.console import console, err_console

_GROUP = "pyfly.cli_plugins"


def _iter_entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=_GROUP))
    except Exception:  # noqa: BLE001 — never let plugin discovery break the CLI
        return []


def discover_cli_plugins() -> list[tuple[str, click.Command]]:
    """Load CLI plugin commands. Bad plugins are skipped with a warning, never fatal."""
    commands: list[tuple[str, click.Command]] = []
    for ep in _iter_entry_points():
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"[warning]![/warning] Failed to load CLI plugin '{ep.name}': {exc}")
            continue
        if isinstance(obj, click.Command):
            commands.append((ep.name, obj))
    return commands


@click.group("plugins")
def plugins_group() -> None:
    """Manage PyFly CLI plugins."""


@plugins_group.command("list")
def list_cmd() -> None:
    """List discovered CLI plugins."""
    found = _iter_entry_points()
    if not found:
        console.print("[dim]No CLI plugins installed.[/dim]")
        return
    for ep in found:
        dist = getattr(getattr(ep, "dist", None), "name", "?")
        console.print(f"  [success]{ep.name}[/success] [dim]({dist})[/dim]")
