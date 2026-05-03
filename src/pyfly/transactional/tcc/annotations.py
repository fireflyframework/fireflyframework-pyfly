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
"""TCC annotations — decorators and parameter injection markers.

Class-level decorators:
    @tcc              — marks a class as a TCC transaction definition
    @tcc_participant  — marks a (nested) class as a TCC participant

Method-level decorators:
    @try_method       — marks a method as the Try phase
    @confirm_method   — marks a method as the Confirm phase
    @cancel_method    — marks a method as the Cancel phase

Parameter injection markers (for use with ``typing.Annotated``):
    FromTry           — inject the try method's result into confirm/cancel methods
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


def tcc(
    name: str,
    timeout_ms: int = 0,
    retry_enabled: bool = False,
    max_retries: int = 0,
    backoff_ms: int = 0,
) -> Callable[[T], T]:
    """Mark a class as a TCC transaction definition.

    Sets ``__pyfly_tcc__`` on the class with the following keys:

    * ``name``          — TCC transaction name
    * ``timeout_ms``    — global timeout in milliseconds (0 = no timeout)
    * ``retry_enabled`` — whether retries are enabled
    * ``max_retries``   — maximum number of retry attempts
    * ``backoff_ms``    — base backoff duration in milliseconds

    Args:
        name: Unique TCC transaction name.
        timeout_ms: Global timeout in milliseconds. Defaults to 0 (no timeout).
        retry_enabled: Whether retries are enabled. Defaults to False.
        max_retries: Maximum retry attempts. Defaults to 0.
        backoff_ms: Base backoff duration in milliseconds. Defaults to 0.
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_tcc__ = {  # type: ignore[attr-defined]
            "name": name,
            "timeout_ms": timeout_ms,
            "retry_enabled": retry_enabled,
            "max_retries": max_retries,
            "backoff_ms": backoff_ms,
        }
        return cls

    return decorator


def tcc_participant(
    id: str,
    order: int = 0,
    timeout_ms: int = 0,
    optional: bool = False,
) -> Callable[[T], T]:
    """Mark a class as a TCC participant.

    Sets ``__pyfly_tcc_participant__`` on the class with the following keys:

    * ``id``         — unique participant identifier
    * ``order``      — execution order (lower runs first)
    * ``timeout_ms`` — participant-level timeout in milliseconds (0 = no timeout)
    * ``optional``   — whether the participant is optional

    Args:
        id: Unique participant identifier.
        order: Execution order (lower values execute first). Defaults to 0.
        timeout_ms: Participant-level timeout in milliseconds. Defaults to 0 (no timeout).
        optional: Whether the participant is optional. Defaults to False.
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_tcc_participant__ = {  # type: ignore[attr-defined]
            "id": id,
            "order": order,
            "timeout_ms": timeout_ms,
            "optional": optional,
        }
        return cls

    return decorator


# ---------------------------------------------------------------------------
# Method-level decorators
# ---------------------------------------------------------------------------


def try_method(
    timeout_ms: int = 0,
    retry: int = 0,
    backoff_ms: int = 0,
) -> Callable[[F], F]:
    """Mark a method as the Try phase of a TCC participant.

    Wraps the function with ``@functools.wraps`` and sets
    ``__pyfly_try_method__`` on the wrapper.

    Args:
        timeout_ms: Execution timeout in milliseconds. Defaults to 0 (no timeout).
        retry: Number of retry attempts on failure. Defaults to 0.
        backoff_ms: Base backoff duration in milliseconds. Defaults to 0.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__pyfly_try_method__ = {  # type: ignore[attr-defined]
            "timeout_ms": timeout_ms,
            "retry": retry,
            "backoff_ms": backoff_ms,
        }
        return wrapper  # type: ignore[return-value]

    return decorator


def confirm_method(
    timeout_ms: int = 0,
    retry: int = 0,
    backoff_ms: int = 0,
) -> Callable[[F], F]:
    """Mark a method as the Confirm phase of a TCC participant.

    Wraps the function with ``@functools.wraps`` and sets
    ``__pyfly_confirm_method__`` on the wrapper.

    Args:
        timeout_ms: Execution timeout in milliseconds. Defaults to 0 (no timeout).
        retry: Number of retry attempts on failure. Defaults to 0.
        backoff_ms: Base backoff duration in milliseconds. Defaults to 0.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__pyfly_confirm_method__ = {  # type: ignore[attr-defined]
            "timeout_ms": timeout_ms,
            "retry": retry,
            "backoff_ms": backoff_ms,
        }
        return wrapper  # type: ignore[return-value]

    return decorator


def cancel_method(
    timeout_ms: int = 0,
    retry: int = 0,
    backoff_ms: int = 0,
) -> Callable[[F], F]:
    """Mark a method as the Cancel phase of a TCC participant.

    Wraps the function with ``@functools.wraps`` and sets
    ``__pyfly_cancel_method__`` on the wrapper.

    Args:
        timeout_ms: Execution timeout in milliseconds. Defaults to 0 (no timeout).
        retry: Number of retry attempts on failure. Defaults to 0.
        backoff_ms: Base backoff duration in milliseconds. Defaults to 0.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__pyfly_cancel_method__ = {  # type: ignore[attr-defined]
            "timeout_ms": timeout_ms,
            "retry": retry,
            "backoff_ms": backoff_ms,
        }
        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Parameter injection markers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FromTry:
    """Inject the result of the try method into confirm/cancel methods.

    Used with ``typing.Annotated`` to declare that a parameter should receive
    the value returned by the participant's try method::

        async def confirm(
            self,
            reservation_id: Annotated[str, FromTry()],
            ctx: TccContext,
        ) -> None:
            ...
    """
