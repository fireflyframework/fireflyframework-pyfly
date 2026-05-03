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
"""Click-based adapter implementing :class:`ShellRunnerPort`."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from io import StringIO
from typing import Any

import click

from pyfly.shell.result import MISSING, ShellParam

# ---- type mapping from Python types to Click parameter types ----

_TYPE_MAP: dict[type, click.types.ParamType] = {
    str: click.STRING,
    int: click.INT,
    float: click.FLOAT,
    bool: click.BOOL,
}


def _build_click_param(sp: ShellParam) -> click.Parameter:
    """Convert a :class:`ShellParam` into a :class:`click.Parameter`."""
    click_type = _TYPE_MAP.get(sp.param_type, click.STRING)

    if sp.is_flag:
        return click.Option(
            [f"--{sp.name.replace('_', '-')}", sp.name],
            is_flag=True,
            default=sp.default if sp.default is not MISSING else False,
            help=sp.help_text or None,
        )

    if sp.is_option:
        kwargs: dict[str, Any] = {
            "type": click_type,
            "help": sp.help_text or None,
        }
        if sp.default is not MISSING:
            kwargs["default"] = sp.default
        else:
            kwargs["required"] = True

        return click.Option(
            [f"--{sp.name.replace('_', '-')}", sp.name],
            **kwargs,
        )

    # Positional argument
    kwargs_arg: dict[str, Any] = {"type": click_type}
    if sp.default is not MISSING:
        kwargs_arg["default"] = sp.default
        kwargs_arg["required"] = False
    return click.Argument([sp.name], **kwargs_arg)


def _wrap_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap *handler* so Click can call it.

    If the handler is an async coroutine function, wrap it so that
    it is executed synchronously.  When a loop is already running
    (e.g. inside ``pytest-asyncio``), the coroutine is scheduled on
    the existing loop; otherwise ``asyncio.run()`` creates a new one.
    """
    if asyncio.iscoroutinefunction(handler):

        @functools.wraps(handler)
        def _sync_wrapper(**kwargs: Any) -> Any:
            coro = handler(**kwargs)
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — safe to use asyncio.run()
                return asyncio.run(coro)
            # Already inside a running loop — run the coroutine via a new
            # thread so we don't block the event loop.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()

        return _sync_wrapper
    return handler


class ClickShellAdapter:
    """Shell runner adapter backed by `click <https://click.palletsprojects.com>`_.

    Implements the :class:`~pyfly.shell.ports.outbound.ShellRunnerPort` protocol.
    """

    def __init__(self, name: str = "app", help_text: str = "") -> None:
        self._root: click.Group = click.Group(name=name, help=help_text or None)
        self._subgroups: dict[str, click.Group] = {}

    # -- ShellRunnerPort interface ------------------------------------------

    def register_command(
        self,
        key: str,
        handler: Callable[..., Any],
        *,
        help_text: str = "",
        group: str = "",
        params: list[ShellParam] | None = None,
    ) -> None:
        """Build a :class:`click.Command` and add it to the root or a sub-group."""
        click_params: list[click.Parameter] = []
        if params:
            for sp in params:
                click_params.append(_build_click_param(sp))

        cmd = click.Command(
            name=key,
            callback=_wrap_handler(handler),
            params=click_params,
            help=help_text or None,
        )

        if group:
            grp = self._subgroups.get(group)
            if grp is None:
                grp = click.Group(name=group)
                self._subgroups[group] = grp
                self._root.add_command(grp)
            grp.add_command(cmd)
        else:
            self._root.add_command(cmd)

    def invoke(self, args: list[str]) -> tuple[int, str]:
        """Invoke the Click group with *args*, returning ``(exit_code, output)``."""
        buf = StringIO()
        try:
            result = self._root.main(  # type: ignore[call-overload]
                args=args,
                standalone_mode=False,
                **{"color": False},
            )
            if isinstance(result, str):
                buf.write(result)
            return 0, buf.getvalue()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            return code, buf.getvalue()
        except click.exceptions.UsageError as exc:
            return 2, str(exc)
        except Exception as exc:
            return 1, str(exc)

    async def run(self, args: list[str] | None = None) -> int:
        """Run the Click group asynchronously, returning the exit code."""
        exit_code, _ = self.invoke(args or [])
        return exit_code

    async def run_interactive(self) -> None:
        """Simple REPL loop: read a line, split, and dispatch via :meth:`invoke`."""
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                continue
            tokens = line.strip().split()
            exit_code, output = self.invoke(tokens)
            if output:
                print(output)
