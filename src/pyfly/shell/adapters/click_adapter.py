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
"""Click-based adapter implementing :class:`ShellRunnerPort`.

Commands run on the application's event loop, the loop the engine, the connection pools, the sessions and
the Mongo client were created on:

- :meth:`ClickShellAdapter.run` (one-shot) and :meth:`ClickShellAdapter.run_interactive` (the REPL) await an
  async ``@shell_method`` on the running loop, where ``PyFlyApplication.run()`` and the ``CommandLineRunner``
  beans run. A task the command starts (an ASYNC workflow, an ``@async_method`` call, a ``detached()``
  write) lives on that loop too: it goes on after the command returns, and the application context drains the
  framework's own background work (workflow runs, scheduler and ``@async_method`` tasks) when it stops.
- The REPL reads each line in a daemon thread, so the loop keeps running scheduled jobs, message consumers
  and orchestration recovery while the operator thinks, and an interrupted session never blocks the process
  exit waiting for a line.
- A command's output is printed; a failing command's message goes to stderr, its traceback to the log, and
  ``run()`` returns its exit code.

The synchronous :meth:`ClickShellAdapter.invoke` is for loop-less use (a script, a sync test): it runs an async
command with ``asyncio.run()``, and refuses one while a loop is running (use :meth:`ClickShellAdapter.ainvoke`
there). Until 26.09.07 an async command called with a running loop ran on a private ``asyncio.run()`` loop in a
worker thread, where the application's engine, pool and Mongo client failed (every backend but SQLite), and the
REPL blocked the application's loop for the whole session.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import threading
from collections.abc import Callable
from io import StringIO
from typing import Any

import click

from pyfly.shell.result import MISSING, ShellParam

logger = logging.getLogger(__name__)

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


async def _read_line(prompt: str) -> str:
    """Read a line with ``input()`` in a daemon thread, keeping the event loop free.

    A daemon thread rather than the loop's default executor: a session interrupted while ``input()`` waits
    (Ctrl-C cancels the application's main task) must not keep the process from exiting until Enter is
    pressed. ``EOFError`` (Ctrl-D) and ``KeyboardInterrupt`` are raised from the await.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def settle(line: str | None, error: BaseException | None) -> None:
        if future.done():  # the awaiting task was cancelled meanwhile
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(line or "")

    def read() -> None:
        try:
            line = input(prompt)
        except BaseException as error:  # noqa: BLE001 — EOFError / KeyboardInterrupt reach the awaiting task
            loop.call_soon_threadsafe(settle, None, error)
        else:
            loop.call_soon_threadsafe(settle, line, None)

    threading.Thread(target=read, name="pyfly-shell-input", daemon=True).start()
    return await future


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
            # Click's callback of an async handler returns the handler's coroutine instead of running it: the
            # adapter awaits it on the running loop (ainvoke), or runs it with asyncio.run() when no loop runs
            # (invoke).
            callback=handler,
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
        """Invoke the Click group with *args*, returning ``(exit_code, output)`` — without a running loop.

        An async command runs with ``asyncio.run()``. With a loop running (inside the application), an async
        command raises :class:`RuntimeError`: it must run on that loop, through :meth:`ainvoke`.
        """
        exit_code, output, pending = self._dispatch(args)
        if pending is None:
            return exit_code, output
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._settle(lambda: asyncio.run(pending))
        pending.close()
        raise RuntimeError(
            "ClickShellAdapter.invoke() cannot run an async command while an event loop is running: the command "
            "would run on another loop than the application's engine, pools and clients. Await "
            "ClickShellAdapter.ainvoke() (or run()) on the running loop instead."
        )

    async def ainvoke(self, args: list[str]) -> tuple[int, str]:
        """Invoke the Click group with *args* on the running loop, returning ``(exit_code, output)``.

        An async command is awaited here, on the application's loop. A command that raises returns exit code 1
        and its message; its traceback is logged.
        """
        exit_code, output, pending = self._dispatch(args)
        if pending is None:
            return exit_code, output
        try:
            result = await pending
        except Exception as exc:  # noqa: BLE001 — a failing command is reported, never propagated
            logger.error("shell_command_failed", extra={"command": args[:1]}, exc_info=True)
            return 1, str(exc)
        return 0, result if isinstance(result, str) else ""

    def _dispatch(self, args: list[str]) -> tuple[int, str, Any]:
        """Parse and run *args* through Click: ``(exit_code, output, None)``, or ``(0, "", coroutine)`` when
        the command is async and its coroutine still has to be awaited."""
        buf = StringIO()
        try:
            result = self._root.main(  # type: ignore[call-overload]
                args=args,
                standalone_mode=False,
                **{"color": False},
            )
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            return code, buf.getvalue(), None
        except click.exceptions.UsageError as exc:
            return 2, str(exc), None
        except Exception as exc:
            logger.error("shell_command_failed", extra={"command": args[:1]}, exc_info=True)
            return 1, str(exc), None
        if inspect.iscoroutine(result):
            return 0, "", result
        if isinstance(result, str):
            buf.write(result)
        return 0, buf.getvalue(), None

    @staticmethod
    def _settle(run: Callable[[], Any]) -> tuple[int, str]:
        try:
            result = run()
        except Exception as exc:  # noqa: BLE001 — a failing command is reported, never propagated
            logger.error("shell_command_failed", exc_info=True)
            return 1, str(exc)
        return 0, result if isinstance(result, str) else ""

    async def run(self, args: list[str] | None = None) -> int:
        """Run one command on the running loop, print its output (a failure's message to stderr), and
        return its exit code."""
        exit_code, output = await self.ainvoke(args or [])
        _print(output, error=exit_code != 0)
        return exit_code

    async def run_interactive(self) -> None:
        """REPL loop: read a line off the loop, split it, and run it on the loop with :meth:`ainvoke`.

        It ends on EOF (Ctrl-D) or Ctrl-C at the prompt.
        """
        while True:
            try:
                line = await _read_line("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                continue
            tokens = line.strip().split()
            exit_code, output = await self.ainvoke(tokens)
            _print(output, error=exit_code != 0)


def _print(output: str, *, error: bool) -> None:
    if output:
        print(output, file=sys.stderr if error else sys.stdout)
