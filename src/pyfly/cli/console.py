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
"""Shared Rich console for CLI output."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.theme import Theme

PYFLY_THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "pyfly": "bold magenta",
        "dim": "dim",
    }
)

console = Console(theme=PYFLY_THEME)


def print_banner() -> None:
    """Print the PyFly ASCII banner with colors."""
    from pyfly import __version__

    banner = (
        "[pyfly]"
        r"                _____.__"
        "\n"
        r"______ ___.__._/ ____\  | ___.__."
        "\n"
        r"\____ <   |  |\   __\|  |<   |  |"
        "\n"
        r"|  |_> >___  | |  |  |  |_\___  |"
        "\n"
        r"|   __// ____| |__|  |____/ ____|"
        "\n"
        r"|__|   \/                 \/"
        "[/pyfly]"
    )
    console.print(banner)
    console.print()
    console.print(f"  [dim]:: PyFly Framework :: (v{__version__})[/dim]")
    console.print("  [dim]Copyright 2026 Firefly Software Foundation. | Apache 2.0 License[/dim]\n")


def print_step_header(step: int, total: int, title: str) -> None:
    """Print a numbered step header: 'Step 1 of 4 — Project Details'."""
    console.print(f"\n  [pyfly]Step {step} of {total}[/pyfly] — [info]{title}[/info]\n")


def print_archetype_table() -> None:
    """Print a Rich table comparing archetypes."""
    from pyfly.cli.templates import ARCHETYPE_DETAILS

    table = Table(title="[pyfly]Archetypes[/pyfly]", border_style="dim", show_lines=True)
    table.add_column("Archetype", style="bold", min_width=12)
    table.add_column("Description", min_width=30)
    table.add_column("Layers", min_width=20)
    table.add_column("Good For", style="dim", min_width=20)

    for key, details in ARCHETYPE_DETAILS.items():
        layers = ", ".join(details["layers"]) if isinstance(details["layers"], list) else details["layers"]
        table.add_row(key, str(details["tagline"]), layers, str(details["good_for"]))

    console.print(table)
    console.print()


def print_feature_summary(features: list[str]) -> None:
    """Print what each selected feature adds."""
    from pyfly.cli.templates import FEATURE_DETAILS

    if not features:
        return
    console.print("  [info]Selected features add:[/info]")
    for feat in features:
        detail = FEATURE_DETAILS.get(feat)
        if detail:
            console.print(f"    [success]•[/success] {feat}: {detail['adds']}")
    console.print()


def print_post_generation_tips(features: list[str]) -> None:
    """Print feature-specific post-generation tips."""
    from pyfly.cli.templates import FEATURE_TIPS

    tips = []
    for feat in features:
        tips.extend(FEATURE_TIPS.get(feat, []))
    if not tips:
        return
    console.print("  [info]Tips:[/info]")
    for tip in tips:
        console.print(f"    [dim]•[/dim] {tip}")
