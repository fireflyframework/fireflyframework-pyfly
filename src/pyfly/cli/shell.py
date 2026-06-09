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
"""'pyfly shell' — interactive REPL with the application context booted."""

from __future__ import annotations

from typing import Any

import click

from pyfly.cli._introspect import boot_context
from pyfly.cli.console import console


def _build_namespace(ctx: Any) -> dict[str, Any]:
    """Build the REPL namespace: ctx, container, and a bean() lookup helper."""

    def bean(key: Any) -> Any:
        return ctx.get_bean(key)

    return {"ctx": ctx, "container": ctx.container, "bean": bean}


def _start_repl(namespace: dict[str, Any]) -> None:
    banner = "PyFly shell — `ctx`, `container`, `bean(T)` available."
    try:
        from ptpython.repl import embed  # type: ignore[import-not-found, import-untyped, unused-ignore]

        embed(globals=namespace, locals=namespace)  # type: ignore[no-untyped-call, unused-ignore]
        return
    except ImportError:
        pass
    try:
        from IPython import start_ipython  # type: ignore[import-not-found, import-untyped, unused-ignore]

        start_ipython(argv=[], user_ns=namespace)  # type: ignore[no-untyped-call, unused-ignore]
        return
    except ImportError:
        pass
    import code

    code.interact(banner=banner, local=namespace)


@click.command("shell")
@click.option("-c", "command", default=None, help="Run a single expression/statement and exit.")
def shell_cmd(command: str | None) -> None:
    """Open an interactive shell with the application context."""
    ctx = boot_context()
    namespace = _build_namespace(ctx)
    if command is not None:
        exec(compile(command, "<pyfly-shell>", "exec"), namespace)  # noqa: S102 — intentional REPL eval
        return
    console.print("[pyfly]PyFly shell[/pyfly] — `ctx`, `container`, `bean(T)` available.")
    _start_repl(namespace)
