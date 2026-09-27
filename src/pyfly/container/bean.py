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

#: The default ``destroy_method`` of :func:`bean` (Spring's ``"(inferred)"``): a bean that its scope
#: destroys (a refresh or another custom scope) and that declares no ``@pre_destroy`` and no
#: ``stop()`` gets the first of ``dispose()``, ``aclose()`` and ``close()`` it has that takes no
#: argument. A singleton infers nothing (see :func:`bean`).
INFER_DESTROY_METHOD = "(inferred)"


@overload
def bean(func: F) -> F: ...


@overload
def bean(
    *,
    name: str = "",
    scope: Scope = Scope.SINGLETON,
    primary: bool = False,
    profile: str = "",
    destroy_method: str = INFER_DESTROY_METHOD,
) -> Callable[[F], F]: ...


def bean(
    func: F | None = None,
    *,
    name: str = "",
    scope: Scope = Scope.SINGLETON,
    primary: bool = False,
    profile: str = "",
    destroy_method: str = INFER_DESTROY_METHOD,
) -> F | Callable[[F], F]:
    """Mark a method inside a @configuration class as a bean factory.

    The return type annotation determines the interface the bean satisfies.

    Args:
        name: Explicit bean name (defaults to the method name).
        scope: Bean scope (default singleton).
        primary: Mark this the primary candidate when several beans share an
            interface — the ``@Bean @Primary`` equivalent.
        profile: Only create this bean when the expression matches the active
            profiles — the ``@Bean @Profile`` equivalent.
        destroy_method: The method of the product the container calls when it destroys
            the bean, after its ``@pre_destroy`` methods (a coroutine is awaited) — the
            ``@Bean(destroyMethod=...)`` equivalent. A singleton is destroyed when the
            context stops (its destroy method after the lifecycle beans stopped), a
            refresh- or custom-scoped bean when its scope evicts it and when the context
            stops; a transient bean is never destroyed. The default,
            :data:`INFER_DESTROY_METHOD`, infers it for a scoped bean only; a singleton's
            product is usually released by its owner in order (the datasource registry
            closes the engines last), so name the method to have a singleton's product
            destroyed. ``""`` declares none.
    """

    def decorator(func: F) -> F:
        func.__pyfly_bean__ = True  # type: ignore[attr-defined]
        func.__pyfly_bean_scope__ = scope  # type: ignore[attr-defined]
        func.__pyfly_bean_destroy_method__ = destroy_method  # type: ignore[attr-defined]
        if name:
            func.__pyfly_bean_name__ = name  # type: ignore[attr-defined]
        if primary:
            func.__pyfly_bean_primary__ = True  # type: ignore[attr-defined]
        if profile:
            func.__pyfly_profile__ = profile  # type: ignore[attr-defined]
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
