# Copyright 2026 Firefly Software Foundation
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

"""Monty worker execution environment (tiers 0 and 1).

`Monty <https://pypi.org/project/pydantic-monty/>`_ is a Rust micro-interpreter
with a deny-by-default capability model: guest code gets no filesystem, network,
or environment access, and reaches the host only through ``external_functions``
explicitly registered per run. It supports a Python *subset* (no third-party
packages) — intended for glue logic over pyfly tools.

The optional ``agentic-script-execution`` extra installs the client and worker binary.
"""

from __future__ import annotations

import asyncio
import inspect
import keyword
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_monty import AsyncSnapshot, ResourceLimits

    from pyfly.agentic.execution.session import ExecutionSession
    from pyfly.agentic.execution.session_types import (
        ExecutionHalt,
        ExecutionReject,
        ExecutionSessionLimits,
        ExecutionValue,
    )

from pyfly.agentic.exceptions import SandboxUnavailableError
from pyfly.agentic.execution.types import Capability, ExecutionLimits, ExecutionResult

logger = logging.getLogger(__name__)


class MontyEnvironment:
    """A sandboxed :class:`ExecutionEnvironment` backed by pydantic-monty.

    Tier 0 (no ``external_functions``) is pure computation. Tier 1 exposes
    allow-listed host callables to the script via ``external_functions`` — the
    basis of pyfly's Code Mode. Each run_code invocation owns and closes its worker pool
    and session; no interpreter state survives into another invocation. Use
    create_session for explicit synchronous persistence.

    Args:
        type_check: Run Monty's static type checker before execution.
        default_limits: Baseline resource ceilings applied to every run (a
            runaway-loop safety net). Per-call ``limits`` are merged over these
            field-by-field. Defaults to a 30s wall-clock timeout.
    """

    def __init__(self, *, type_check: bool = False, default_limits: ExecutionLimits | None = None) -> None:
        try:
            import pydantic_monty  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise SandboxUnavailableError(
                "MontyEnvironment requires the optional 'pyfly[agentic-script-execution]' extra"
            ) from exc
        self._type_check = type_check
        self._default_limits = default_limits if default_limits is not None else ExecutionLimits(timeout_seconds=30.0)

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.COMPUTE, Capability.EXTERNAL_FUNCTIONS})

    def create_session(
        self,
        *,
        external_functions: Mapping[str, Callable[..., ExecutionValue | ExecutionHalt | ExecutionReject]] | None = None,
        limits: ExecutionSessionLimits | None = None,
    ) -> ExecutionSession:
        """Open an explicit synchronous persistent session on the calling thread.

        Callbacks stay on that thread; the caller must close the session or use
        its context manager. Session limits are independent of one-shot defaults.
        No interpreter state is shared with ``run_code`` or another session.
        Persistent ``type_check=True`` fails before worker creation because
        SDK 0.0.23 loses checker context after suspended callbacks; the one-shot
        asynchronous checker is unchanged.
        """
        from pyfly.agentic.execution.monty_session import MontyExecutionSession

        return MontyExecutionSession(
            external_functions=external_functions,
            limits=limits,
            type_check=self._type_check,
        )

    def _effective_limits(self, limits: ExecutionLimits | None) -> ExecutionLimits:
        base = self._default_limits
        if limits is None:
            return base
        # Per-call fields override the defaults; unset fields keep the safety net.
        return ExecutionLimits(
            timeout_seconds=limits.timeout_seconds if limits.timeout_seconds is not None else base.timeout_seconds,
            max_memory_bytes=limits.max_memory_bytes if limits.max_memory_bytes is not None else base.max_memory_bytes,
            max_allocations=limits.max_allocations if limits.max_allocations is not None else base.max_allocations,
            max_recursion_depth=(
                limits.max_recursion_depth if limits.max_recursion_depth is not None else base.max_recursion_depth
            ),
        )

    def _resource_limits(self, limits: ExecutionLimits | None) -> ResourceLimits | None:
        eff = self._effective_limits(limits)
        if eff.max_allocations is not None:
            raise ValueError("max_allocations is not supported by Monty 0.0.23; use memory and time limits")
        kwargs: ResourceLimits = {}
        if eff.timeout_seconds is not None:
            kwargs["max_duration_secs"] = eff.timeout_seconds
        if eff.max_memory_bytes is not None:
            kwargs["max_memory"] = eff.max_memory_bytes
        if eff.max_recursion_depth is not None:
            kwargs["max_recursion_depth"] = eff.max_recursion_depth
        return kwargs or None

    async def run_code(
        self,
        code: str,
        *,
        inputs: dict[str, Any] | None = None,
        external_functions: dict[str, Callable[..., Any]] | None = None,
        limits: ExecutionLimits | None = None,
    ) -> ExecutionResult:
        from pydantic_monty import AsyncMonty, CollectStreams, MontyError

        functions = external_functions or {}
        for name in (inputs or {}).keys() | functions.keys():
            if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError("Input and external function names must be valid Python identifiers")
        if any(not callable(fn) for fn in functions.values()):
            raise ValueError("External functions must be callable")
        resource_limits = self._resource_limits(limits)
        timeout = self._effective_limits(limits).timeout_seconds
        collector = CollectStreams()
        checked_out = False
        try:
            async with (
                asyncio.timeout(timeout),
                AsyncMonty(min_processes=0, max_processes=1, request_timeout=timeout) as pool,
                pool.checkout(
                    limits=resource_limits,
                    type_check=self._type_check,
                    type_check_stubs=_type_stubs(inputs or {}, functions) if self._type_check else None,
                ) as session,
            ):
                checked_out = True
                snapshot = await session.feed_start(
                    code, inputs=inputs or None, external_lookup=functions or None, print_callback=collector
                )
                output = await _drive(snapshot, functions)
        except (MontyError, TimeoutError, RuntimeError, OSError) as exc:
            if (
                not checked_out
                and isinstance(exc, RuntimeError | OSError)
                and not isinstance(exc, MontyError | TimeoutError)
            ):
                raise SandboxUnavailableError(
                    "Monty runtime could not start; install 'pyfly[agentic-script-execution]' "
                    "and verify the configured MONTY_BIN executable"
                ) from exc
            stdout, stderr = _split_streams(collector)
            return ExecutionResult(
                success=False, error=str(exc), error_type=type(exc).__name__, stdout=stdout, stderr=stderr
            )

        stdout, stderr = _split_streams(collector)
        return ExecutionResult(success=True, output=output, stdout=stdout, stderr=stderr)


def _type_stubs(inputs: dict[str, Any], functions: dict[str, Callable[..., Any]]) -> str:
    names = inputs.keys() | functions.keys()
    alias = "_PyflyHostAny"
    while alias in names:
        alias += "_"
    lines = [f"from typing import Any as {alias}"]
    for name in sorted(names):
        if name in inputs:
            lines.append(f"{name}: {alias}")
        else:
            lines.append(f"def {name}(*args: {alias}, **kwargs: {alias}) -> {alias}: ...")
    lines.append(f"__all__ = {sorted(names)!r}")
    return "\n".join(lines) + "\n"


async def _drive(snapshot: AsyncSnapshot, functions: dict[str, Callable[..., Any]]) -> Any:
    from pydantic_monty import AsyncFunctionSnapshot, AsyncNameLookupSnapshot, MontyComplete

    while not isinstance(snapshot, MontyComplete):
        if isinstance(snapshot, AsyncFunctionSnapshot):
            fn = functions.get(snapshot.function_name)
            if snapshot.is_os_function:
                snapshot = await snapshot.resume_not_handled()
                continue
            if snapshot.object_id is not None:
                snapshot = await snapshot.resume({"exception": AttributeError("Host object access is unavailable")})
                continue
            if fn is None:
                snapshot = await snapshot.resume(
                    {"exception": NameError(f"External function {snapshot.function_name!r} is not registered")}
                )
                continue
            try:
                result = (
                    await fn(*snapshot.args, **snapshot.kwargs)
                    if inspect.iscoroutinefunction(fn)
                    else await asyncio.to_thread(fn, *snapshot.args, **snapshot.kwargs)
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                snapshot = await snapshot.resume({"exception": exc})
            else:
                # Answer with a value so existing scripts need no `await` for host tools.
                snapshot = await snapshot.resume({"return_value": result})
        elif isinstance(snapshot, AsyncNameLookupSnapshot):
            snapshot = (
                await snapshot.resume_auto()
                if snapshot.object_id is None and snapshot.variable_name in functions
                else await snapshot.resume()
            )
        else:
            snapshot = await snapshot.resume_auto()
    return snapshot.output


def _split_streams(collector: Any) -> tuple[str, str]:
    out: list[str] = []
    err: list[str] = []
    for stream, text in collector.output:
        (out if stream == "stdout" else err).append(text)
    return "".join(out), "".join(err)
