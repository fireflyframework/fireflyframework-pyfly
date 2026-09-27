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
"""Container types and enums."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from typing import Any, Protocol, runtime_checkable


class Scope(Enum):
    """Bean lifecycle scope."""

    SINGLETON = auto()
    TRANSIENT = auto()
    REQUEST = auto()
    SESSION = auto()


# A bean's scope: a built-in :class:`Scope` or a custom scope name registered via
# ``Container.register_scope``.
ScopeSpec = Scope | str


def scope_name(scope: ScopeSpec) -> str:
    """Display name for a scope — the enum member name or the custom scope string."""
    return scope.name if isinstance(scope, Scope) else str(scope)


@runtime_checkable
class ScopeHandler(Protocol):
    """SPI for a custom bean scope (Spring's ``org.springframework...config.Scope``).

    Register an implementation with ``Container.register_scope(name, handler)`` and declare
    beans with that scope name (``@component(scope="my-scope")`` or
    ``register(cls, scope="my-scope")``).
    """

    def get(self, name: str, object_factory: Callable[[], Any]) -> Any:
        """Return the cached instance for *name*, or create it via *object_factory*
        (called at most once), cache it, and return it."""
        ...

    def remove(self, name: str) -> Any | None:
        """Evict *name* from the scope, returning the removed instance or ``None``."""
        ...


class NoAutowire:
    """Marker: the container never injects a parameter annotated ``Annotated[T, NoAutowire]``.

    The parameter keeps its default, even when a bean of type ``T`` exists. Use it for a constructor
    argument that callers may pass by hand but that dependency injection must leave alone::

        class Repository(Generic[T, ID]):
            def __init__(self, model=None, session: Annotated[AsyncSession | None, NoAutowire] = None): ...

    A DI-built ``Repository`` therefore never receives the transient ``AsyncSession`` bean, while
    ``Repository(Order, session)`` in a test still works. The marker is used as the class itself
    (``NoAutowire``) or as an instance (``NoAutowire()``). A marked parameter without a default cannot be
    built by the container, which reports it as a missing dependency.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "NoAutowire"


def is_no_autowire(metadata: object) -> bool:
    """Whether *metadata* (an ``Annotated`` extra) is the :class:`NoAutowire` marker."""
    return metadata is NoAutowire or isinstance(metadata, NoAutowire)
