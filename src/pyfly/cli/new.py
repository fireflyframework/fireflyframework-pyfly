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
"""'pyfly new' command for scaffolding projects."""

from __future__ import annotations

import keyword
import re
import sys
from pathlib import Path

import click
import questionary
from questionary import Choice, Separator, Style
from rich.panel import Panel
from rich.tree import Tree

from pyfly.cli.console import (
    console,
    print_archetype_table,
    print_feature_summary,
    print_post_generation_tips,
    print_step_header,
)
from pyfly.cli.templates import (
    ARCHETYPE_DESCRIPTIONS,
    ARCHETYPE_DETAILS,
    AVAILABLE_FEATURES,
    DEFAULT_FEATURES,
    FEATURE_DETAILS,
    FEATURE_GROUPS,
    generate_project,
)

PYFLY_STYLE = Style(
    [
        ("qmark", "fg:#b388ff bold"),
        ("question", "bold"),
        ("answer", "fg:#4fc3f7 bold"),
        ("pointer", "fg:#b388ff bold"),
        ("highlighted", "fg:#b388ff bold"),
        ("selected", "fg:#66bb6a bold"),
        ("separator", "fg:#cc5454"),
        ("instruction", "fg:#757575"),
        ("text", ""),
        ("disabled", "fg:#858585 italic"),
    ]
)

_ARCHETYPES = list(ARCHETYPE_DESCRIPTIONS.keys())

# Python stdlib modules and common names that will cause import conflicts.
_RESERVED_NAMES: set[str] = {
    "test",
    "tests",
    "src",
    "pyfly",
    "site",
    "setup",
    "pip",
    "pkg",
    "main",
    "app",
    "config",
    "logging",
    "typing",
    "collections",
    "os",
    "sys",
    "io",
    "re",
    "json",
    "http",
    "email",
    "html",
    "xml",
    "csv",
    "ast",
    "dis",
    "code",
    "types",
    "abc",
    "dataclasses",
    "enum",
    "string",
    "textwrap",
    "unicodedata",
    "struct",
    "codecs",
    "datetime",
    "calendar",
    "time",
    "math",
    "random",
    "statistics",
    "pathlib",
    "glob",
    "shutil",
    "sqlite3",
    "socket",
    "signal",
    "subprocess",
    "threading",
    "multiprocessing",
    "asyncio",
    "uuid",
    "hashlib",
    "secrets",
    "pydantic",
    "starlette",
    "uvicorn",
    "fastapi",
    "granian",
    "hypercorn",
    "sqlalchemy",
    "alembic",
    "click",
    "rich",
    "jinja2",
}


def _validate_project_name(name: str) -> str | None:
    """Validate project name. Returns error message or None if valid."""
    if not name:
        return "Project name cannot be empty."
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", name):
        return "Project name must start with a letter and contain only letters, digits, hyphens, and underscores."
    pkg = name.replace("-", "_").replace(" ", "_").lower()
    if keyword.iskeyword(pkg):
        return f"'{pkg}' is a Python keyword and cannot be used as a package name."
    if pkg in _RESERVED_NAMES:
        return f"'{name}' conflicts with a Python stdlib module or common package. Choose a different name."
    if pkg in sys.stdlib_module_names:
        return f"'{name}' conflicts with Python stdlib module '{pkg}'. Choose a different name."
    return None


def _prompt_interactive() -> tuple[str, str, str, list[str]]:
    """Run interactive wizard with arrow-key navigation and space-bar toggling."""
    total_steps = 4

    try:
        console.print(Panel("[pyfly]  PyFly Project Generator  [/pyfly]", border_style="magenta"))

        # ── Step 1 of 4: Project Details ──
        print_step_header(1, total_steps, "Project Details")

        name = questionary.text(
            "Project name:",
            validate=lambda val: True if _validate_project_name(val) is None else _validate_project_name(val),
            style=PYFLY_STYLE,
        ).unsafe_ask()

        default_pkg = name.replace("-", "_").replace(" ", "_").lower()
        package_name = questionary.text(
            "Package name:",
            default=default_pkg,
            style=PYFLY_STYLE,
        ).unsafe_ask()

        # ── Step 2 of 4: Architecture ──
        print_step_header(2, total_steps, "Architecture")
        print_archetype_table()

        archetype = questionary.select(
            "Select archetype:",
            choices=[
                Choice(
                    title=f"{key:12s}  {ARCHETYPE_DETAILS[key]['tagline']}",
                    value=key,
                )
                for key in _ARCHETYPES
            ],
            style=PYFLY_STYLE,
            instruction="(use arrow keys)",
        ).unsafe_ask()

        # ── Step 3 of 4: Features ──
        if archetype == "library":
            features: list[str] = []
        else:
            print_step_header(3, total_steps, "Features")
            console.print("  [dim]Features are optional PyFly packages. Each maps to an installable extra.[/dim]\n")

            defaults = DEFAULT_FEATURES[archetype]
            choices: list[Choice | Separator] = []
            for group_name, group_feats in FEATURE_GROUPS:
                choices.append(Separator(f"── {group_name} ──"))
                for feat in group_feats:
                    detail = FEATURE_DETAILS.get(feat, {})
                    short = detail.get("short", "")
                    choices.append(
                        Choice(
                            title=f"{feat:16s} {short}",
                            value=feat,
                            checked=feat in defaults,
                        )
                    )

            features = questionary.checkbox(
                "Select features:",
                choices=choices,
                style=PYFLY_STYLE,
                instruction="(space to toggle, enter to confirm)",
            ).unsafe_ask()

        # ── Step 4 of 4: Review & Create ──
        print_step_header(4, total_steps, "Review & Create")

        archetype_desc = ARCHETYPE_DETAILS[archetype]["tagline"]
        summary = (
            f"  [info]Name:[/info]      {name}\n"
            f"  [info]Package:[/info]   {package_name}\n"
            f"  [info]Archetype:[/info] {archetype} — {archetype_desc}\n"
            f"  [info]Features:[/info]  {', '.join(features) if features else 'none'}"
        )
        console.print(Panel(summary, title="[pyfly]Project Summary[/pyfly]", border_style="cyan"))
        print_feature_summary(features)

        if not questionary.confirm(
            "Create this project?",
            default=True,
            style=PYFLY_STYLE,
        ).unsafe_ask():
            raise SystemExit(0)

        console.print()
        return name, package_name, archetype, features

    except (KeyboardInterrupt, EOFError):
        console.print("\n[warning]Cancelled.[/warning]")
        raise SystemExit(0) from None


@click.command()
@click.argument("name", required=False)
@click.option(
    "--archetype",
    type=click.Choice(_ARCHETYPES),
    default=None,
    help="Project archetype.",
)
@click.option(
    "--features",
    "features_str",
    default=None,
    help="Comma-separated PyFly extras (e.g. web,data,cache).",
)
@click.option(
    "--directory",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="Parent directory for the new project.",
)
def new_command(name: str | None, archetype: str | None, features_str: str | None, directory: str) -> None:
    """Create a new PyFly project."""
    # Interactive mode when no name provided
    if name is None:
        name, _pkg, archetype, features = _prompt_interactive()
    else:
        # Validate name in non-interactive mode
        error = _validate_project_name(name)
        if error:
            console.print(f"[error]{error}[/error]")
            raise SystemExit(1)

        archetype = archetype or "core"
        if features_str is not None:
            features = [f.strip() for f in features_str.split(",") if f.strip()]
        else:
            features = list(DEFAULT_FEATURES[archetype])

    # Validate features
    invalid = set(features) - set(AVAILABLE_FEATURES)
    if invalid:
        console.print(f"[error]Unknown features: {', '.join(sorted(invalid))}[/error]")
        console.print(f"[dim]Available: {', '.join(AVAILABLE_FEATURES)}[/dim]")
        raise SystemExit(1)

    parent = Path(directory)
    project_dir = parent / name

    if project_dir.exists():
        console.print(f"[error]Directory '{project_dir}' already exists.[/error]")
        raise SystemExit(1)

    generate_project(name, project_dir, archetype, features)

    # Build a tree showing created structure
    tree = Tree(f"[success]{name}/[/success]")
    for path in sorted(project_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(project_dir)
            tree.add(f"[dim]{relative}[/dim]")

    panel = Panel(
        tree,
        title=f"[success]Created {archetype} project[/success]",
        border_style="green",
    )
    console.print(panel)

    # Post-generation next steps
    console.print("\n  [info]Next steps:[/info]")
    console.print(f"    cd {project_dir}")
    console.print("    uv sync --group dev")
    if archetype == "cli":
        console.print(f"    python -m {name.replace('-', '_').replace(' ', '_').lower()}.main")
    elif archetype != "library":
        console.print("    pyfly run --reload")
    if archetype == "web":
        console.print("\n  [info]Pages:[/info]")
        console.print("    http://localhost:8080/")
        console.print("    http://localhost:8080/about")
    if archetype == "fastapi-api":
        console.print("\n  [info]API Docs:[/info]")
        console.print("    http://localhost:8080/docs   (Swagger UI)")
        console.print("    http://localhost:8080/redoc  (ReDoc)")
    console.print()
    print_post_generation_tips(features)
    console.print()
