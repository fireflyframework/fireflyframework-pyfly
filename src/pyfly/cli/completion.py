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
"""'pyfly completion' & 'pyfly upgrade'."""

from __future__ import annotations

import shutil
import subprocess

import click

from pyfly.cli.console import console, err_console


def _root_cli() -> click.Group:
    from pyfly.cli.main import cli

    return cli


@click.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion_cmd(shell: str) -> None:
    """Print the shell completion script.

    Install it by sourcing the output, e.g.:
      bash:  eval "$(pyfly completion bash)"   (add to ~/.bashrc)
      zsh:   eval "$(pyfly completion zsh)"    (add to ~/.zshrc)
      fish:  pyfly completion fish | source    (add to fish config)
    """
    from click.shell_completion import get_completion_class

    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        err_console.print(f"[error]✗[/error] Unsupported shell: {shell}")
        raise SystemExit(1)
    comp = comp_cls(cli=_root_cli(), ctx_args={}, prog_name="pyfly", complete_var="_PYFLY_COMPLETE")
    click.echo(comp.source())


@click.command("upgrade")
def upgrade_cmd() -> None:
    """Upgrade the installed pyfly package."""
    if shutil.which("uv") is not None:
        cmd = ["uv", "pip", "install", "--upgrade", "pyfly"]
    elif shutil.which("pip") is not None:
        cmd = ["pip", "install", "--upgrade", "pyfly"]
    else:
        err_console.print("[error]✗[/error] Neither uv nor pip found.")
        raise SystemExit(1)
    console.print(f"[info]Upgrading pyfly via {cmd[0]}…[/info]")
    raise SystemExit(subprocess.run(cmd).returncode)  # noqa: S603
