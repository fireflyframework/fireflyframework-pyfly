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
"""Provider[T] — deferred dependency lookup (the Spring ObjectFactory/Provider equivalent)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar, cast

if TYPE_CHECKING:
    from pyfly.container.container import Container

T = TypeVar("T")


class Provider(Generic[T]):
    """A lazy handle to a bean — call :meth:`get` (or the instance) to resolve it.

    Inject ``Provider[Foo]`` instead of ``Foo`` to defer resolution: each
    :meth:`get` returns a freshly-resolved bean, so a singleton can obtain new
    TRANSIENT instances, and construction-time cycles/expensive beans can be
    deferred until first use.

    Usage::

        @service
        class Worker:
            def __init__(self, jobs: Provider[Job]) -> None:
                self._jobs = jobs

            def run(self) -> None:
                job = self._jobs.get()  # fresh Job each call (if Job is TRANSIENT)
    """

    __slots__ = ("_container", "_cls")

    def __init__(self, container: Container, cls: type[T]) -> None:
        self._container = container
        self._cls = cls

    def get(self) -> T:
        """Resolve and return the bean (a fresh instance for TRANSIENT scope).

        ``T`` may be what a constructor parameter may be: a parametrized generic
        (``Provider[async_sessionmaker[AsyncSession]]`` resolves the ``async_sessionmaker`` bean, as
        the parameter ``async_sessionmaker[AsyncSession]`` does), ``X | None``, ``list[X]`` or an
        ``Annotated`` qualifier. A parametrized ``T`` used to be looked up as a registration key,
        which it never is, so ``get()`` raised ``NoSuchBeanError`` for a bean that existed.
        """
        if isinstance(self._cls, type):
            return self._container.resolve(self._cls)
        return cast(T, self._container._resolve_param(self._cls))

    def __call__(self) -> T:
        return self.get()
