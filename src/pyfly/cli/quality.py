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
"""'pyfly test/lint/format/typecheck' — thin wrappers over the project's tools."""

from __future__ import annotations

import shutil
import subprocess

import click

from pyfly.cli.console import err_console


def _run_tool(tool: str, args: list[str], *, install_hint: str) -> None:
    if shutil.which(tool) is None:
        err_console.print(f"[error]✗[/error] '{tool}' not found. {install_hint}")
        raise SystemExit(1)
    result = subprocess.run([tool, *args])  # noqa: S603
    raise SystemExit(result.returncode)


@click.command("test", context_settings={"ignore_unknown_options": True})
@click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
def test_cmd(pytest_args: tuple[str, ...]) -> None:
    """Run the test suite (pytest)."""
    _run_tool("pytest", list(pytest_args), install_hint="Install dev tools (e.g. uv sync --group dev).")


@click.command("lint", context_settings={"ignore_unknown_options": True})
@click.argument("ruff_args", nargs=-1, type=click.UNPROCESSED)
def lint_cmd(ruff_args: tuple[str, ...]) -> None:
    """Lint the project (ruff check)."""
    _run_tool("ruff", ["check", *ruff_args], install_hint="Install ruff (e.g. uv sync --group dev).")


@click.command("format")
@click.option("--check", is_flag=True, help="Check formatting without writing changes.")
@click.argument("paths", nargs=-1, type=click.Path())
def format_cmd(check: bool, paths: tuple[str, ...]) -> None:
    """Format the project (ruff format)."""
    args = ["format", *(["--check"] if check else []), *paths]
    _run_tool("ruff", args, install_hint="Install ruff (e.g. uv sync --group dev).")


@click.command("typecheck", context_settings={"ignore_unknown_options": True})
@click.argument("mypy_args", nargs=-1, type=click.UNPROCESSED)
def typecheck_cmd(mypy_args: tuple[str, ...]) -> None:
    """Type-check the project (mypy)."""
    target = list(mypy_args) or ["src"]
    _run_tool("mypy", target, install_hint="Install mypy (e.g. uv sync --group dev).")
