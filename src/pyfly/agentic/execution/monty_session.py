# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Synchronous, thread-owned public Monty session implementation."""

from __future__ import annotations

import inspect
import keyword
import math
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from pyfly.agentic.exceptions import SandboxUnavailableError
from pyfly.agentic.execution.monty_env import _split_streams
from pyfly.agentic.execution.session_types import (
    ExecutionFeedback,
    ExecutionFeedResult,
    ExecutionFeedStatus,
    ExecutionHalt,
    ExecutionReject,
    ExecutionSessionError,
    ExecutionSessionLimits,
    ExecutionSessionState,
    ExecutionSessionUsage,
    ExecutionValue,
)
from pyfly.agentic.execution.session_values import copy_value
from pyfly.agentic.execution.types import Capability, ExecutionLimits, ExecutionResult

if TYPE_CHECKING:
    from pydantic_monty import CollectStreams, SyncSnapshot

type _Callback = Callable[..., ExecutionValue | ExecutionHalt | ExecutionReject]

_BUILTIN_EXCEPTIONS = (
    Exception,
    ValueError,
    TypeError,
    RuntimeError,
    NameError,
    AttributeError,
    PermissionError,
    LookupError,
    KeyError,
    IndexError,
    ArithmeticError,
    ZeroDivisionError,
    OverflowError,
    OSError,
    FileNotFoundError,
    TimeoutError,
    MemoryError,
    RecursionError,
    NotImplementedError,
    AssertionError,
    StopIteration,
    EOFError,
)


class _SessionFault(Exception):
    """A terminal driver/capability/resource failure, never guest-recoverable."""


def _positive(value: object, name: str, *, integer: bool = False) -> None:
    if type(value) not in ((int,) if integer else (int, float)):
        raise ValueError(f"{name} must be a positive finite {'integer' if integer else 'number'}")
    maximum = sys.maxsize if integer else threading.TIMEOUT_MAX
    if cast(float, value) > maximum:
        raise ValueError(f"{name} exceeds the native-safe ceiling {maximum}")
    try:
        finite = math.isfinite(cast(float, value))
    except OverflowError:
        finite = False
    if not finite or cast(float, value) <= 0:
        raise ValueError(f"{name} must be positive and finite")


def _limits_snapshot(limits: ExecutionSessionLimits) -> ExecutionSessionLimits:
    defaults = ExecutionSessionLimits().execution
    raw = limits.execution
    if raw.max_allocations is not None:
        raise ValueError("max_allocations is not supported by Monty 0.0.23; use memory and time limits")
    execution = ExecutionLimits(
        timeout_seconds=raw.timeout_seconds if raw.timeout_seconds is not None else defaults.timeout_seconds,
        max_memory_bytes=raw.max_memory_bytes if raw.max_memory_bytes is not None else defaults.max_memory_bytes,
        max_recursion_depth=(
            raw.max_recursion_depth if raw.max_recursion_depth is not None else defaults.max_recursion_depth
        ),
    )
    _positive(execution.timeout_seconds, "timeout_seconds")
    _positive(execution.max_memory_bytes, "max_memory_bytes", integer=True)
    _positive(execution.max_recursion_depth, "max_recursion_depth", integer=True)
    _positive(limits.max_session_seconds, "max_session_seconds")
    for name in ("max_feeds", "max_external_calls", "max_suspensions", "max_output_bytes", "max_value_bytes"):
        _positive(getattr(limits, name), name, integer=True)
    return replace(limits, execution=execution)


def _identifier(name: object) -> None:
    if type(name) is not str or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("Input and external function names must be valid Python identifiers")


class MontyExecutionSession:
    """One persistent Rust worker; use ``MontyEnvironment.create_session``.

    Only explicitly registered callbacks can reach the host. Callback execution
    is direct on the creating thread. No mounting, host-object dispatch, retry,
    checkpoint restoration or implicit worker replacement is exposed.
    """

    def __init__(
        self,
        *,
        external_functions: Mapping[str, _Callback] | None = None,
        limits: ExecutionSessionLimits | None = None,
        type_check: bool = False,
    ) -> None:
        if type_check:
            raise ValueError(
                "Persistent type_check=True is unavailable: Monty SDK 0.0.23 loses static context "
                "after suspended host callbacks; use the one-shot run_code checker instead"
            )
        from pydantic_monty import Monty

        self._limits = _limits_snapshot(limits if limits is not None else ExecutionSessionLimits())
        self._functions = dict(external_functions or {})
        for name, function in self._functions.items():
            _identifier(name)
            if (
                not callable(function)
                or inspect.iscoroutinefunction(function)
                or inspect.iscoroutinefunction(cast(Any, function).__call__)
            ):
                raise ValueError("External functions must be synchronous callables")
        self._owner = threading.current_thread()
        self._state = ExecutionSessionState.READY
        self._created = time.monotonic()
        self._ended: float | None = None
        self._feeds = self._calls = self._suspensions = self._output_bytes = 0
        self._feedback: list[ExecutionFeedback] = []
        self._stack = ExitStack()
        execution = self._limits.execution
        try:
            pool = self._stack.enter_context(
                Monty(
                    min_processes=0,
                    max_processes=1,
                    request_timeout=execution.timeout_seconds,
                    checkout_timeout=execution.timeout_seconds,
                )
            )
            self._session = self._stack.enter_context(
                pool.checkout(
                    limits={
                        "max_duration_secs": execution.timeout_seconds,
                        "max_memory": execution.max_memory_bytes,
                        "max_recursion_depth": execution.max_recursion_depth,
                        "max_suspensions": self._limits.max_suspensions,
                    },
                    type_check=False,
                )
            )
        except BaseException as exc:
            self._stack.close()
            if isinstance(exc, Exception):
                raise SandboxUnavailableError(
                    "Persistent execution requires 'pyfly[agentic-script-execution]' and its Monty runtime binary"
                ) from exc
            raise

    def _check_owner(self) -> None:
        if threading.current_thread() is not self._owner:
            raise ExecutionSessionError("Execution session belongs to its creating thread")

    @property
    def state(self) -> ExecutionSessionState:
        self._check_owner()
        return self._state

    @property
    def capabilities(self) -> frozenset[Capability]:
        self._check_owner()
        return frozenset({Capability.COMPUTE, Capability.EXTERNAL_FUNCTIONS})

    @property
    def usage(self) -> ExecutionSessionUsage:
        self._check_owner()
        return ExecutionSessionUsage(
            self._feeds,
            self._calls,
            self._suspensions,
            self._output_bytes,
            (self._ended if self._ended is not None else time.monotonic()) - self._created,
        )

    def __enter__(self) -> Self:
        self._check_owner()
        if self._state != ExecutionSessionState.READY:
            raise ExecutionSessionError("Execution session is not ready")
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            self.close()
        except ExecutionSessionError as cleanup:
            if exc is None:
                raise
            exc.add_note(str(cleanup))

    def _finish(self, state: ExecutionSessionState) -> str | None:
        self._state = state
        try:
            self._stack.close()
        except Exception as exc:
            self._state = ExecutionSessionState.BROKEN
            return str(exc)
        finally:
            self._ended = time.monotonic()
        return None

    def close(self) -> None:
        self._check_owner()
        if self._state == ExecutionSessionState.RUNNING:
            raise ExecutionSessionError("Execution session operations are not reentrant")
        if self._state in (ExecutionSessionState.CLOSED, ExecutionSessionState.HALTED, ExecutionSessionState.BROKEN):
            return
        error = self._finish(ExecutionSessionState.CLOSED)
        if error is not None:
            raise ExecutionSessionError(f"Execution cleanup failed: {error}")

    def _check_lifetime(self) -> None:
        if time.monotonic() - self._created >= self._limits.max_session_seconds:
            raise _SessionFault("Execution session exceeded max_session_seconds")

    def _copy(self, value: object) -> ExecutionValue:
        try:
            return copy_value(value, self._limits.max_value_bytes)
        except ValueError as exc:
            raise _SessionFault(str(exc)) from exc

    def feed(self, code: str, *, inputs: Mapping[str, ExecutionValue] | None = None) -> ExecutionFeedResult:
        from pydantic_monty import CollectStreams, MontyRuntimeError, MontySyntaxError, MontyTypingError

        self._check_owner()
        if self._state != ExecutionSessionState.READY:
            raise ExecutionSessionError(f"Execution session does not admit feeds in state {self._state}")
        if type(code) is not str:
            raise ValueError("Code must be a string")
        values = dict(inputs) if inputs is not None else {}
        for name in values:
            _identifier(name)
            if name in self._functions:
                raise ValueError("Inputs cannot shadow registered external functions")
        safe_inputs = cast(dict[str, ExecutionValue], copy_value(values, self._limits.max_value_bytes))
        self._feedback = []
        collector = CollectStreams(max_bytes=max(1, self._limits.max_output_bytes - self._output_bytes))
        output: ExecutionValue = None
        status = ExecutionFeedStatus.ERROR
        error: str | None = None
        error_type: str | None = None
        self._state = ExecutionSessionState.RUNNING
        try:
            self._check_lifetime()
            if self._feeds >= self._limits.max_feeds:
                raise _SessionFault("Execution session exceeded max_feeds")
            self._feeds += 1
            snapshot = self._session.feed_start(
                code,
                inputs=safe_inputs or None,
                external_lookup=self._functions or None,
                print_callback=collector,
            )
            self._check_lifetime()
            value = self._drive(snapshot)
            if isinstance(value, ExecutionHalt):
                status = ExecutionFeedStatus.HALTED
                output = value.value
                expired = time.monotonic() - self._created >= self._limits.max_session_seconds
                error = self._finish(ExecutionSessionState.BROKEN if expired else ExecutionSessionState.HALTED)
                error_type = "ExecutionCleanupError" if error is not None else None
                if expired:
                    error = "Accepted halt exceeded max_session_seconds" + (f"; cleanup: {error}" if error else "")
                    error_type = error_type or "ExecutionSessionError"
            else:
                self._check_lifetime()
                status = ExecutionFeedStatus.REJECTED if self._feedback else ExecutionFeedStatus.COMPLETED
                output = None if self._feedback else self._copy(value)
                self._check_lifetime()
                self._state = ExecutionSessionState.READY
        except (MontySyntaxError, MontyTypingError, MontyRuntimeError) as exc:
            underlying = exc.exception()
            expired = time.monotonic() - self._created >= self._limits.max_session_seconds
            if (
                expired
                or self._suspensions >= self._limits.max_suspensions
                or isinstance(underlying, MemoryError | RecursionError | TimeoutError)
            ):
                cleanup = self._finish(ExecutionSessionState.BROKEN)
                error = f"{exc}; cleanup: {cleanup}" if cleanup else str(exc)
                if expired:
                    error = f"Execution session exceeded max_session_seconds; guest error: {error}"
                status = ExecutionFeedStatus.ERROR
            else:
                self._state = ExecutionSessionState.READY
                error = str(exc)
                status = ExecutionFeedStatus.REJECTED if self._feedback else ExecutionFeedStatus.ERROR
            error_type = type(exc).__name__
        except Exception as exc:
            status = ExecutionFeedStatus.ERROR
            cleanup = self._finish(ExecutionSessionState.BROKEN)
            error = f"{exc}; cleanup: {cleanup}" if cleanup else str(exc)
            error_type = "ExecutionSessionError" if isinstance(exc, _SessionFault) else type(exc).__name__
        except BaseException:
            self._finish(ExecutionSessionState.BROKEN)
            raise
        return self._result(status, output, error, error_type, collector)

    def _result(
        self,
        status: ExecutionFeedStatus,
        output: ExecutionValue,
        error: str | None,
        error_type: str | None,
        collector: CollectStreams,
    ) -> ExecutionFeedResult:
        stdout, stderr = _split_streams(collector)
        self._output_bytes += len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
        if status == ExecutionFeedStatus.REJECTED and error is None:
            error = self._feedback[0].message
            error_type = "ExecutionRejected"
        result = ExecutionResult(
            success=status in (ExecutionFeedStatus.COMPLETED, ExecutionFeedStatus.HALTED) and error is None,
            output=output,
            stdout=stdout,
            stderr=stderr,
            error=error,
            error_type=error_type,
        )
        return ExecutionFeedResult(status, result, self._state, tuple(self._feedback), self.usage)

    def _drive(self, snapshot: SyncSnapshot) -> ExecutionValue | ExecutionHalt:
        from pydantic_monty import FunctionSnapshot, MontyComplete, NameLookupSnapshot

        while not isinstance(snapshot, MontyComplete):
            self._check_lifetime()
            if self._suspensions >= self._limits.max_suspensions:
                raise _SessionFault("Execution session exceeded max_suspensions")
            self._suspensions += 1
            if isinstance(snapshot, FunctionSnapshot):
                if snapshot.is_os_function:
                    snapshot = snapshot.resume_not_handled()
                elif snapshot.object_id is not None:
                    snapshot = snapshot.resume({"exception": AttributeError("Host object access is unavailable")})
                elif snapshot.function_name not in self._functions:
                    snapshot = snapshot.resume({"exception": NameError("External function is not registered")})
                else:
                    value = self._call(snapshot.function_name, snapshot.args, snapshot.kwargs)
                    if isinstance(value, ExecutionHalt):
                        return value
                    self._check_lifetime()
                    if isinstance(value, ExecutionReject):
                        snapshot = snapshot.resume({"exception": ValueError(value.message)})
                    elif isinstance(value, Exception):
                        snapshot = snapshot.resume({"exception": value})
                    else:
                        snapshot = snapshot.resume({"return_value": value})
            elif isinstance(snapshot, NameLookupSnapshot):
                snapshot = (
                    snapshot.resume_auto()
                    if snapshot.object_id is None and snapshot.variable_name in self._functions
                    else snapshot.resume()
                )
            else:
                raise _SessionFault("Unsupported asynchronous execution snapshot")
            self._check_lifetime()
        return cast(ExecutionValue, snapshot.output)

    def _call(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> ExecutionValue | ExecutionHalt | ExecutionReject | Exception:
        if self._calls >= self._limits.max_external_calls:
            raise _SessionFault("Execution session exceeded max_external_calls")
        self._calls += 1
        if self._feedback:
            return PermissionError("Host callbacks are unavailable while draining a rejected feed")
        safe_args, safe_kwargs = cast(
            tuple[tuple[ExecutionValue, ...], dict[str, ExecutionValue]], self._copy((args, kwargs))
        )
        self._check_lifetime()
        try:
            value = self._functions[name](*safe_args, **safe_kwargs)
        except Exception as exc:
            self._check_lifetime()
            return self._guest_exception(exc)
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise _SessionFault("Persistent execution callbacks must return synchronous values")
        if type(value) is ExecutionHalt:
            return ExecutionHalt(self._copy(value.value))
        if type(value) is ExecutionReject:
            self._copy((value.code, value.message))
            if type(value.code) is not str or not value.code or type(value.message) is not str or not value.message:
                raise _SessionFault("ExecutionReject requires a nonempty string code and message")
            self._feedback.append(ExecutionFeedback(value.code, value.message, name, self._calls))
            self._check_lifetime()
            return value
        self._check_lifetime()
        return self._copy(value)

    def _guest_exception(self, exc: Exception) -> Exception:
        # Read native args without invoking an application subclass' properties
        # or __str__, and never pass the original host exception to the SDK.
        args = cast(tuple[ExecutionValue, ...], self._copy(cast(Any, BaseException.args).__get__(exc)))
        kind = next((allowed for allowed in _BUILTIN_EXCEPTIONS if type(exc) is allowed), RuntimeError)
        fresh = kind(*args)
        self._copy((args, str(fresh)))
        return fresh
