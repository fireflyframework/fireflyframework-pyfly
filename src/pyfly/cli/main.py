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

import click

from pyfly.cli.console import print_banner


class PyFlyCLI(click.Group):
    """Custom Click group that shows the PyFly banner on help."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        print_banner()
        super().format_help(ctx, formatter)


@click.group(cls=PyFlyCLI)
@click.version_option(package_name="pyfly")
def cli() -> None:
    """PyFly — The official Python implementation of the Firefly Framework."""


# Import and register commands (lazy to avoid heavy imports)
from pyfly.cli.db import db_group  # noqa: E402
from pyfly.cli.doctor import doctor_command  # noqa: E402
from pyfly.cli.generate import generate_group  # noqa: E402
from pyfly.cli.info import info_command  # noqa: E402
from pyfly.cli.license import license_command  # noqa: E402
from pyfly.cli.new import new_command  # noqa: E402
from pyfly.cli.run import run_command  # noqa: E402
from pyfly.cli.sbom import sbom_command  # noqa: E402

cli.add_command(new_command, name="new")
cli.add_command(db_group, name="db")
cli.add_command(generate_group, name="generate")
cli.add_command(generate_group, name="g")
cli.add_command(info_command, name="info")
cli.add_command(run_command, name="run")
cli.add_command(doctor_command, name="doctor")
cli.add_command(license_command, name="license")
cli.add_command(sbom_command, name="sbom")
