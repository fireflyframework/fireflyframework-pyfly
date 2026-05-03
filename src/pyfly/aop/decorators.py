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
"""AOP decorators — @aspect and advice annotations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.container.types import Scope

T = TypeVar("T", bound=type)
F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# @aspect — marks a class as an AOP aspect
# ---------------------------------------------------------------------------


def aspect(cls: T) -> T:
    """Mark a class as a PyFly aspect.

    Sets the following metadata on the class:

    * ``__pyfly_aspect__``     = True
    * ``__pyfly_injectable__`` = True
    * ``__pyfly_stereotype__`` = "aspect"
    * ``__pyfly_scope__``      = Scope.SINGLETON
    """
    cls.__pyfly_aspect__ = True  # type: ignore[attr-defined]
    cls.__pyfly_injectable__ = True  # type: ignore[attr-defined]
    cls.__pyfly_stereotype__ = "aspect"  # type: ignore[attr-defined]
    cls.__pyfly_scope__ = Scope.SINGLETON  # type: ignore[attr-defined]
    return cls


# ---------------------------------------------------------------------------
# Advice decorators — @before, @after_returning, @after_throwing, @after, @around
# ---------------------------------------------------------------------------


def _make_advice(advice_type: str) -> Callable[[str], Callable[[F], F]]:
    """Create an advice decorator factory for the given *advice_type*.

    The returned factory takes a pointcut pattern string and returns a
    decorator that annotates the wrapped method with:

    * ``__pyfly_advice_type__`` — e.g. ``"before"``, ``"around"``
    * ``__pyfly_pointcut__``    — the pointcut expression string
    """

    def factory(pointcut: str) -> Callable[[F], F]:
        def decorator(fn: F) -> F:
            fn.__pyfly_advice_type__ = advice_type  # type: ignore[attr-defined]
            fn.__pyfly_pointcut__ = pointcut  # type: ignore[attr-defined]
            return fn

        return decorator

    return factory


before = _make_advice("before")
after_returning = _make_advice("after_returning")
after_throwing = _make_advice("after_throwing")
after = _make_advice("after")
around = _make_advice("around")
