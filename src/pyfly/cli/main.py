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
"""PyFly CLI — Project scaffolding and code generation."""

from __future__ import annotations

import os
from typing import Any

import click

from pyfly.cli.console import err_console, print_banner


class PyFlyCLI(click.Group):
    """Custom Click group that shows the PyFly banner on help and prints clean
    errors instead of raw Python tracebacks."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        print_banner()
        super().format_help(ctx, formatter)

    def invoke(self, ctx: click.Context) -> Any:
        """Run the command, surfacing failures as a clean ``Error: ...`` line.

        Click's own exceptions (usage errors, ``Abort``) and explicit
        ``SystemExit`` codes pass through unchanged. Any other exception —
        e.g. a configuration/validation error during app boot — is printed as
        a single user-friendly message with exit code 1, instead of dumping a
        traceback. Pass ``--debug`` (or set ``PYFLY_DEBUG=1``) to see the full
        traceback.
        """
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Abort, click.exceptions.Exit, SystemExit):
            # Click's own control flow: usage errors, Ctrl-C, and ctx.exit()/--help
            # (Exit carries the intended exit code, including 0 for --help).
            raise
        except Exception as exc:
            debug = bool(ctx.params.get("debug")) or os.environ.get("PYFLY_DEBUG", "").lower() in (
                "1",
                "true",
                "yes",
            )
            err_console.print(f"[error]Error:[/error] {exc}")
            if debug:
                err_console.print_exception()
            else:
                err_console.print("[dim]Re-run with --debug (or PYFLY_DEBUG=1) for the full traceback.[/dim]")
            raise SystemExit(1) from exc


@click.group(cls=PyFlyCLI)
@click.option(
    "--debug",
    is_flag=True,
    envvar="PYFLY_DEBUG",
    help="Show full tracebacks on error (default: clean one-line errors).",
)
@click.version_option(package_name="pyfly")
def cli(debug: bool) -> None:  # noqa: ARG001 — read from ctx.params in PyFlyCLI.invoke
    """PyFly — The official Python implementation of the Firefly Framework."""


# Import and register commands (lazy to avoid heavy imports)
from pyfly.cli.build import build_group  # noqa: E402
from pyfly.cli.completion import completion_cmd, upgrade_cmd  # noqa: E402
from pyfly.cli.db import db_group  # noqa: E402
from pyfly.cli.doctor import doctor_command  # noqa: E402
from pyfly.cli.features import add_cmd, features_cmd, remove_cmd  # noqa: E402
from pyfly.cli.generate import generate_group  # noqa: E402
from pyfly.cli.info import info_command  # noqa: E402
from pyfly.cli.introspect_cmds import (  # noqa: E402
    actuator_cmd,
    beans_cmd,
    conditions_cmd,
    env_cmd,
    health_cmd,
    metrics_cmd,
    routes_cmd,
)
from pyfly.cli.license import license_command  # noqa: E402
from pyfly.cli.new import new_command  # noqa: E402
from pyfly.cli.openapi import openapi_cmd  # noqa: E402
from pyfly.cli.plugins import discover_cli_plugins, plugins_group  # noqa: E402
from pyfly.cli.quality import format_cmd, lint_cmd, test_cmd, typecheck_cmd  # noqa: E402
from pyfly.cli.run import run_command  # noqa: E402
from pyfly.cli.sbom import sbom_command  # noqa: E402
from pyfly.cli.shell import shell_cmd  # noqa: E402

cli.add_command(new_command, name="new")
cli.add_command(db_group, name="db")
cli.add_command(generate_group, name="generate")
cli.add_command(generate_group, name="g")
cli.add_command(info_command, name="info")
cli.add_command(run_command, name="run")
cli.add_command(doctor_command, name="doctor")
cli.add_command(license_command, name="license")
cli.add_command(sbom_command, name="sbom")
cli.add_command(routes_cmd, name="routes")
cli.add_command(beans_cmd, name="beans")
cli.add_command(env_cmd, name="env")
cli.add_command(health_cmd, name="health")
cli.add_command(metrics_cmd, name="metrics")
cli.add_command(conditions_cmd, name="conditions")
cli.add_command(actuator_cmd, name="actuator")
cli.add_command(shell_cmd, name="shell")
cli.add_command(openapi_cmd, name="openapi")
cli.add_command(test_cmd, name="test")
cli.add_command(lint_cmd, name="lint")
cli.add_command(format_cmd, name="format")
cli.add_command(typecheck_cmd, name="typecheck")
cli.add_command(features_cmd, name="features")
cli.add_command(add_cmd, name="add")
cli.add_command(remove_cmd, name="remove")
cli.add_command(build_group, name="build")
cli.add_command(completion_cmd, name="completion")
cli.add_command(upgrade_cmd, name="upgrade")
cli.add_command(plugins_group, name="plugins")

# Third-party CLI plugins (entry-point group 'pyfly.cli_plugins').
for _name, _command in discover_cli_plugins():
    cli.add_command(_command, name=_name)
