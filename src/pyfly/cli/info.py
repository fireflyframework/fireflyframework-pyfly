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
"""'pyfly info' — Display framework and environment information."""

from __future__ import annotations

import platform
import sys

import click
from rich.table import Table

from pyfly import __version__
from pyfly.cli.console import console

_EXTRAS = [
    ("web", "starlette"),
    ("data-relational", "sqlalchemy"),
    ("data-document", "beanie"),
    ("eda", "aiokafka"),
    ("kafka", "aiokafka"),
    ("rabbitmq", "aio_pika"),
    ("redis", "redis"),
    ("cache", "redis"),
    ("client", "httpx"),
    ("observability", "prometheus_client"),
    ("security", "jwt"),
    ("scheduling", "croniter"),
    ("cli", "rich"),
]


def _check_extra(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


@click.command()
def info_command() -> None:
    """Display PyFly framework and environment information."""
    console.print(f"\n[pyfly]PyFly Framework[/pyfly] [dim]v{__version__}[/dim]\n")

    # Environment table
    env_table = Table(title="Environment", show_header=False, border_style="dim")
    env_table.add_column("Key", style="info")
    env_table.add_column("Value")
    env_table.add_row("Python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    env_table.add_row("Platform", platform.platform())
    env_table.add_row("Architecture", platform.machine())
    console.print(env_table)

    # Installed extras table
    extras_table = Table(title="\nInstalled Extras", border_style="dim")
    extras_table.add_column("Extra", style="info")
    extras_table.add_column("Status")

    for extra_name, check_module in _EXTRAS:
        installed = _check_extra(check_module)
        status = "[success]installed[/success]" if installed else "[dim]not installed[/dim]"
        extras_table.add_row(extra_name, status)

    console.print(extras_table)
    console.print()
