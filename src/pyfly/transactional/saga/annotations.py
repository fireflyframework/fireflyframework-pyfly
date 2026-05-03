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
"""Saga annotations — decorators and parameter injection markers.

Class-level decorators:
    @saga             — marks a class as a saga definition
    @compensation_step — marks a class as a compensation step for a saga
    @external_step    — marks a class as an external (non-method) saga step

Method-level decorators:
    @saga_step        — marks a method as a saga step
    @step_event       — marks a method/function as the event handler for a step

Parameter injection markers (for use with ``typing.Annotated``):
    Input, FromStep, Header, Variable, SetVariable, FromCompensationResult
    Headers, Variables, CompensationError  (singleton sentinels)
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T", bound=type)
F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Class-level decorators
# ---------------------------------------------------------------------------


def saga(
    name: str,
    layer_concurrency: int = 0,
) -> Callable[[T], T]:
    """Mark a class as a saga definition.

    Sets ``__pyfly_saga__`` on the class with the following keys:

    * ``name``              — saga name
    * ``layer_concurrency`` — max concurrent steps per dependency layer (0 = unlimited)

    Args:
        name: Unique saga name.
        layer_concurrency: Maximum parallel steps per layer. Defaults to 0 (unlimited).
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_saga__ = {  # type: ignore[attr-defined]
            "name": name,
            "layer_concurrency": layer_concurrency,
        }
        return cls

    return decorator


def compensation_step(
    saga: str,
    for_step_id: str,
) -> Callable[[T], T]:
    """Mark a class as a compensation step for a saga.

    Sets ``__pyfly_compensation_step__`` on the class with the following keys:

    * ``saga``        — name of the parent saga
    * ``for_step_id`` — id of the forward step this class compensates

    Args:
        saga: Name of the parent saga.
        for_step_id: The step id whose compensation this class implements.
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_compensation_step__ = {  # type: ignore[attr-defined]
            "saga": saga,
            "for_step_id": for_step_id,
        }
        return cls

    return decorator


def external_step(
    saga: str,
    id: str,
    compensate: str | None = None,
    depends_on: list[str] | None = None,
    retry: int = 0,
    backoff_ms: int = 0,
    timeout_ms: int = 0,
    jitter: bool = False,
    jitter_factor: float = 0.0,
    cpu_bound: bool = False,
    compensation_retry: int | None = None,
    compensation_backoff_ms: int | None = None,
    compensation_timeout_ms: int | None = None,
    compensation_critical: bool = False,
) -> Callable[[T], T]:
    """Mark a class as an external saga step (implemented outside the saga class).

    Sets ``__pyfly_external_step__`` on the class.

    Args:
        saga: Name of the parent saga.
        id: Unique step identifier within the saga.
        compensate: Optional id of the compensation step.
        depends_on: Optional list of step ids this step depends on.
        retry: Number of retry attempts on failure. Defaults to 0.
        backoff_ms: Base backoff duration in milliseconds. Defaults to 0.
        timeout_ms: Step execution timeout in milliseconds. Defaults to 0 (no timeout).
        jitter: Whether to add jitter to backoff. Defaults to False.
        jitter_factor: Fraction of backoff to use as jitter range. Defaults to 0.0.
        cpu_bound: Whether the step should run in a thread/process pool. Defaults to False.
        compensation_retry: Override retry count for the compensation step.
        compensation_backoff_ms: Override backoff for the compensation step.
        compensation_timeout_ms: Override timeout for the compensation step.
        compensation_critical: If True, saga failure is raised if compensation fails.
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_external_step__ = {  # type: ignore[attr-defined]
            "saga": saga,
            "id": id,
            "compensate": compensate,
            "depends_on": depends_on,
            "retry": retry,
            "backoff_ms": backoff_ms,
            "timeout_ms": timeout_ms,
            "jitter": jitter,
            "jitter_factor": jitter_factor,
            "cpu_bound": cpu_bound,
            "compensation_retry": compensation_retry,
            "compensation_backoff_ms": compensation_backoff_ms,
            "compensation_timeout_ms": compensation_timeout_ms,
            "compensation_critical": compensation_critical,
        }
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Method-level decorators
# ---------------------------------------------------------------------------


def saga_step(
    id: str,
    compensate: str | None = None,
    depends_on: list[str] | None = None,
    retry: int = 0,
    backoff_ms: int = 0,
    timeout_ms: int = 0,
    jitter: bool = False,
    jitter_factor: float = 0.0,
    cpu_bound: bool = False,
    idempotency_key: str | None = None,
    compensation_retry: int | None = None,
    compensation_backoff_ms: int | None = None,
    compensation_timeout_ms: int | None = None,
    compensation_critical: bool = False,
) -> Callable[[F], F]:
    """Mark a method as a saga step.

    Wraps the function with ``@functools.wraps`` and sets
    ``__pyfly_saga_step__`` on the wrapper.

    Args:
        id: Unique step identifier within the saga.
        compensate: Optional id of the compensation step to invoke on failure.
        depends_on: Step ids this step must wait for. Defaults to [].
        retry: Number of retry attempts on failure. Defaults to 0.
        backoff_ms: Base backoff duration in milliseconds. Defaults to 0.
        timeout_ms: Step execution timeout in milliseconds. Defaults to 0 (no timeout).
        jitter: Whether to add jitter to backoff. Defaults to False.
        jitter_factor: Fraction of backoff to use as jitter range. Defaults to 0.0.
        cpu_bound: Whether the step should run in a thread/process pool. Defaults to False.
        idempotency_key: Template string for deduplication key. Defaults to None.
        compensation_retry: Override retry count for the compensation step.
        compensation_backoff_ms: Override backoff for the compensation step.
        compensation_timeout_ms: Override timeout for the compensation step.
        compensation_critical: If True, saga failure is raised if compensation fails.
    """
    resolved_depends_on: list[str] = depends_on if depends_on is not None else []

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__pyfly_saga_step__ = {  # type: ignore[attr-defined]
            "id": id,
            "compensate": compensate,
            "depends_on": resolved_depends_on,
            "retry": retry,
            "backoff_ms": backoff_ms,
            "timeout_ms": timeout_ms,
            "jitter": jitter,
            "jitter_factor": jitter_factor,
            "cpu_bound": cpu_bound,
            "idempotency_key": idempotency_key,
            "compensation_retry": compensation_retry,
            "compensation_backoff_ms": compensation_backoff_ms,
            "compensation_timeout_ms": compensation_timeout_ms,
            "compensation_critical": compensation_critical,
        }
        return wrapper  # type: ignore[return-value]

    return decorator


def step_event(
    topic: str,
    event_type: str,
    key: str | None = None,
) -> Callable[[F], F]:
    """Declare the event emitted or consumed by a saga step.

    Sets ``__pyfly_step_event__`` directly on the function (no wrapper).

    Args:
        topic: Kafka/messaging topic name.
        event_type: Logical event type identifier.
        key: Optional message key expression. Defaults to None.
    """

    def decorator(func: F) -> F:
        func.__pyfly_step_event__ = {  # type: ignore[attr-defined]
            "topic": topic,
            "event_type": event_type,
            "key": key,
        }
        return func

    return decorator


# ---------------------------------------------------------------------------
# Parameter injection markers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Input:
    """Inject the saga input payload (or a specific key from it).

    Args:
        key: Optional dot-path key within the input payload. If None the
             entire input object is injected.
    """

    key: str | None = None


@dataclass(frozen=True)
class FromStep:
    """Inject the result produced by a previous saga step.

    Args:
        step_id: Id of the step whose result should be injected.
    """

    step_id: str


@dataclass(frozen=True)
class Header:
    """Inject a single message/request header value.

    Args:
        name: Header name (case-insensitive matching is implementation-defined).
    """

    name: str


@dataclass(frozen=True)
class Variable:
    """Inject a saga-scoped variable by name (read-only).

    Args:
        name: Variable name.
    """

    name: str


@dataclass(frozen=True)
class SetVariable:
    """Mark a parameter as a variable to be set in saga scope.

    Args:
        name: Variable name to write the parameter value into.
    """

    name: str


@dataclass(frozen=True)
class FromCompensationResult:
    """Inject the result of a compensation action from a previous step.

    Args:
        step_id: Id of the step whose compensation result should be injected.
    """

    step_id: str


# ---------------------------------------------------------------------------
# Singleton sentinels
# ---------------------------------------------------------------------------


class _HeadersSentinel:
    """Inject the full headers mapping."""

    _instance: _HeadersSentinel | None = None

    def __new__(cls) -> _HeadersSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Headers"


class _VariablesSentinel:
    """Inject the full saga variables mapping."""

    _instance: _VariablesSentinel | None = None

    def __new__(cls) -> _VariablesSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "Variables"


class _CompensationErrorSentinel:
    """Inject the exception raised during the forward step that triggered compensation."""

    _instance: _CompensationErrorSentinel | None = None

    def __new__(cls) -> _CompensationErrorSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "CompensationError"


#: Inject the complete headers mapping.
Headers: _HeadersSentinel = _HeadersSentinel()

#: Inject the complete saga variables mapping.
Variables: _VariablesSentinel = _VariablesSentinel()

#: Inject the exception that triggered the current compensation.
CompensationError: _CompensationErrorSentinel = _CompensationErrorSentinel()
