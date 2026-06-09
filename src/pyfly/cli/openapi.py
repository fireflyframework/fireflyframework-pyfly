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
"""'pyfly openapi' — export the application's OpenAPI schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from pyfly.cli._introspect import boot_context
from pyfly.cli.console import console


def _build_spec(ctx: Any) -> dict[str, Any]:
    """Collect route metadata from the booted context and produce an OpenAPI 3.1 spec dict.

    Uses Approach A: ``ControllerRegistrar.collect_route_metadata(ctx)`` feeds
    ``OpenAPIGenerator(title, version).generate(route_metadata)``, mirroring what
    ``create_app()`` does in the Starlette adapter.
    """
    from pyfly.web.adapters.starlette.controller import ControllerRegistrar
    from pyfly.web.openapi import OpenAPIGenerator

    title: str = str(ctx.config.get("pyfly.app.name", "PyFly"))
    version: str = str(ctx.config.get("pyfly.app.version", "0.1.0"))
    description: str = str(ctx.config.get("pyfly.app.description", ""))

    route_metadata = ControllerRegistrar().collect_route_metadata(ctx)
    generator = OpenAPIGenerator(title=title, version=version, description=description)
    return generator.generate(route_metadata or None)


@click.command("openapi")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "yaml"]),
    default="json",
    help="Output format.",
)
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(),
    default=None,
    help="Write to a file instead of stdout.",
)
def openapi_cmd(fmt: str, output: str | None) -> None:
    """Export the OpenAPI schema for this application."""
    ctx = boot_context()
    spec = _build_spec(ctx)
    if fmt == "yaml":
        import yaml  # type: ignore[import-untyped]

        text = yaml.safe_dump(spec, sort_keys=False)
    else:
        text = json.dumps(spec, indent=2)
    if output:
        Path(output).write_text(text)
        console.print(f"[success]✓[/success] Wrote OpenAPI spec to {output}")
    else:
        click.echo(text)
