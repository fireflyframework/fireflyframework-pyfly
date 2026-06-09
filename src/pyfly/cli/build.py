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
"""'pyfly build' — package the application (wheel/sdist), build an image, or stamp build info."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import click

from pyfly.cli.console import console, err_console


def _require(tool: str, hint: str) -> None:
    if shutil.which(tool) is None:
        err_console.print(f"[error]✗[/error] '{tool}' not found. {hint}")
        raise SystemExit(1)


@click.group("build")
def build_group() -> None:
    """Build and package the application."""


@build_group.command("wheel")
def wheel_cmd() -> None:
    """Build a wheel (uv build --wheel)."""
    _require("uv", "Install uv: https://docs.astral.sh/uv/")
    raise SystemExit(subprocess.run(["uv", "build", "--wheel"]).returncode)  # noqa: S603, S607


@build_group.command("sdist")
def sdist_cmd() -> None:
    """Build a source distribution (uv build --sdist)."""
    _require("uv", "Install uv: https://docs.astral.sh/uv/")
    raise SystemExit(subprocess.run(["uv", "build", "--sdist"]).returncode)  # noqa: S603, S607


@build_group.command("info")
@click.option("-o", "--output", "output", default="build-info.json", type=click.Path(), help="Output path.")
def info_cmd(output: str) -> None:
    """Write build-info.json (git SHA, build timestamp) for /actuator/info."""
    import datetime

    sha = ""
    if shutil.which("git") is not None:
        proc = subprocess.run(  # noqa: S603, S607
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        sha = proc.stdout.strip() if proc.returncode == 0 else ""
    data = {
        "git": {"sha": sha},
        "build": {"time": datetime.datetime.now(datetime.UTC).isoformat()},
    }
    Path(output).write_text(json.dumps(data, indent=2))
    console.print(f"[success]✓[/success] Wrote {output}")


@build_group.command("image")
@click.option("--tag", "-t", "tag", default=None, help="Image tag (default: project name).")
@click.option("--builder", type=click.Choice(["pack", "docker"]), default="pack", help="Image builder.")
def image_cmd(tag: str | None, builder: str) -> None:
    """Build an OCI image (Cloud Native Buildpacks via 'pack', or Docker)."""
    image = tag or "pyfly-app:latest"
    if builder == "pack":
        _require("pack", "Install the Cloud Native Buildpacks CLI: https://buildpacks.io/docs/tools/pack/")
        cmd = ["pack", "build", image, "--builder", "paketobuildpacks/builder-jammy-base"]
    else:
        _require("docker", "Install Docker: https://docs.docker.com/get-docker/")
        cmd = ["docker", "build", "-t", image, "."]
    raise SystemExit(subprocess.run(cmd).returncode)  # noqa: S603
