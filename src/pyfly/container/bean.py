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
"""@bean factory methods, @primary marker, and Qualifier for disambiguation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

from pyfly.container.types import Scope

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T", bound=type)


@overload
def bean(func: F) -> F: ...


@overload
def bean(
    *,
    name: str = "",
    scope: Scope = Scope.SINGLETON,
) -> Callable[[F], F]: ...


def bean(
    func: F | None = None,
    *,
    name: str = "",
    scope: Scope = Scope.SINGLETON,
) -> F | Callable[[F], F]:
    """Mark a method inside a @configuration class as a bean factory.

    The return type annotation determines the interface the bean satisfies.
    """

    def decorator(func: F) -> F:
        func.__pyfly_bean__ = True  # type: ignore[attr-defined]
        func.__pyfly_bean_scope__ = scope  # type: ignore[attr-defined]
        if name:
            func.__pyfly_bean_name__ = name  # type: ignore[attr-defined]
        return func

    if func is not None:
        return decorator(func)
    return decorator


def primary(cls: T) -> T:
    """Mark a class as the primary implementation when multiple candidates exist."""
    cls.__pyfly_primary__ = True  # type: ignore[attr-defined]
    return cls


class Qualifier:
    """Used with typing.Annotated to select a specific named bean.

    Usage::

        def __init__(self, db: Annotated[DataSource, Qualifier("primary_db")]):
            ...
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"Qualifier({self.name!r})"
