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
"""'pyfly add/remove/features' — manage PyFly feature extras in a project."""

from __future__ import annotations

import re
from pathlib import Path

import click

from pyfly.cli.console import console, err_console
from pyfly.cli.templates import AVAILABLE_FEATURES, FEATURE_DETAILS, FEATURE_TIPS

# Matches a quoted ``pyfly`` requirement: optional ``[extras]`` and an optional
# trailing version/marker (which must start with a PEP 508 separator so we never
# match a *different* package like ``"pyfly-extensions"``). The trailing part is
# preserved when rewriting extras.
_PYFLY_DEP_RE = re.compile(r'"pyfly(?:\[(?P<extras>[^\]]*)\])?(?P<rest>(?:[<>=!~;\s][^"]*)?)"')


def _update_pyfly_extras(dep: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> str:
    """Return the ``"pyfly[...]"`` dependency string with extras added/removed.

    Any trailing version specifier / environment marker (e.g. ``>=26.0``) is
    preserved.
    """
    match = _PYFLY_DEP_RE.fullmatch(dep.strip())
    if not match:
        return dep
    extras = {e.strip() for e in (match.group("extras") or "").split(",") if e.strip()}
    extras |= set(add or [])
    extras -= set(remove or [])
    rest = match.group("rest") or ""
    if not extras:
        return f'"pyfly{rest}"'
    return f'"pyfly[{",".join(sorted(extras))}]{rest}"'


def _project_root(ctx: click.Context) -> Path:
    cwd = (ctx.obj or {}).get("cwd") if ctx.obj else None
    start = Path(cwd) if cwd else Path.cwd()
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    err_console.print("[error]✗[/error] No pyproject.toml found.")
    raise SystemExit(1)


def _patch_pyproject(root: Path, *, add: list[str] | None = None, remove: list[str] | None = None) -> bool:
    path = root / "pyproject.toml"
    text = path.read_text()
    new_text, n = _PYFLY_DEP_RE.subn(lambda m: _update_pyfly_extras(m.group(0), add=add, remove=remove), text)
    if n == 0:
        err_console.print("[error]✗[/error] Could not find a 'pyfly[...]' dependency to update.")
        return False
    path.write_text(new_text)
    return True


def _validate(names: tuple[str, ...]) -> list[str]:
    invalid = [n for n in names if n not in AVAILABLE_FEATURES]
    if invalid:
        err_console.print(f"[error]✗[/error] Unknown feature(s): {', '.join(invalid)}")
        err_console.print(f"[dim]Available: {', '.join(AVAILABLE_FEATURES)}[/dim]")
        raise SystemExit(1)
    return list(names)


@click.command("features")
@click.pass_context
def features_cmd(ctx: click.Context) -> None:
    """List available PyFly features and which are enabled in this project."""
    root = _project_root(ctx)
    text = (root / "pyproject.toml").read_text()
    match = _PYFLY_DEP_RE.search(text)
    enabled: set[str] = set()
    if match and match.group("extras"):
        enabled = {e.strip() for e in match.group("extras").split(",") if e.strip()}
    for feat in AVAILABLE_FEATURES:
        mark = "[success]✓[/success]" if feat in enabled else "[dim]·[/dim]"
        short = FEATURE_DETAILS.get(feat, {}).get("short", "")
        console.print(f"  {mark} [info]{feat:16s}[/info] [dim]{short}[/dim]")


@click.command("add")
@click.argument("features", nargs=-1, required=True)
@click.pass_context
def add_cmd(ctx: click.Context, features: tuple[str, ...]) -> None:
    """Add PyFly feature extra(s) to this project."""
    names = _validate(features)
    root = _project_root(ctx)
    if not _patch_pyproject(root, add=names):
        raise SystemExit(1)
    console.print(f"[success]✓[/success] Added: {', '.join(names)}")
    console.print("[dim]Run 'uv sync' to install.[/dim]")
    for feat in names:
        for tip in FEATURE_TIPS.get(feat, []):
            console.print(f"  [dim]•[/dim] {tip}")


@click.command("remove")
@click.argument("features", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.pass_context
def remove_cmd(ctx: click.Context, features: tuple[str, ...], yes: bool) -> None:
    """Remove PyFly feature extra(s) from this project."""
    names = _validate(features)
    if not yes:
        click.confirm(f"Remove {', '.join(names)} from this project?", abort=True)
    root = _project_root(ctx)
    if not _patch_pyproject(root, remove=names):
        raise SystemExit(1)
    console.print(f"[success]✓[/success] Removed: {', '.join(names)}")
    console.print("[dim]Run 'uv sync' to apply.[/dim]")
