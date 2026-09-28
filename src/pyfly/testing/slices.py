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
"""Test slice decorators for focused testing of specific layers.

:func:`DataTest` is Spring's ``@DataJpaTest``: with PyFly's pytest plugin (installed with PyFly, the
``pyfly`` pytest11 entry point) every test of the class runs in a data slice whose units of work roll back
when the test ends. :func:`WebTest` and :func:`ServiceTest` only mark a class (:func:`get_test_slice`); build
those slices with :func:`~pyfly.testing.slice_context.web_slice` and
:func:`~pyfly.testing.slice_context.service_slice`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
    from pyfly.core.config import Config

_SLICE_MARKER = "__pyfly_test_slice__"
_DATA_TEST = "__pyfly_data_test__"


@dataclass(frozen=True)
class DataTestOptions:
    """What a ``@DataTest`` class (or a ``@pytest.mark.data_test`` test) asks for: the slice's *beans*, its
    *config* (``None``: the ``pyfly_data_config`` fixture's), *overrides*, whether the test's units of work
    roll back, and on which *datasources* (``None``: the default one)."""

    beans: tuple[type, ...] = ()
    config: Config | None = None
    overrides: Mapping[type, Any] = field(default_factory=dict)
    rollback: bool = True
    datasources: tuple[str, ...] | None = None


def WebTest(cls: type) -> type:  # noqa: N802
    """Mark test class as a web-layer test slice (build it with ``web_slice``)."""
    setattr(cls, _SLICE_MARKER, "web")
    return cls


@overload
def DataTest(cls: type, /) -> type: ...  # noqa: N802


@overload
def DataTest(  # noqa: N802
    cls: None = None,
    /,
    *,
    beans: Iterable[type] = (),
    config: Config | None = None,
    overrides: Mapping[type, Any] | None = None,
    rollback: bool = True,
    datasources: Iterable[str] | None = None,
) -> Callable[[type], type]: ...


def DataTest(  # noqa: N802
    cls: type | None = None,
    /,
    *,
    beans: Iterable[type] = (),
    config: Config | None = None,
    overrides: Mapping[type, Any] | None = None,
    rollback: bool = True,
    datasources: Iterable[str] | None = None,
) -> type | Callable[[type], type]:
    """Make a test class a data-layer test slice (Spring's ``@DataJpaTest``), bare or with options::

        @DataTest(beans=[UserRepository, UserService])
        class TestUsers:
            async def test_saves(self, data_context: ApplicationContext) -> None:
                users = data_context.get_bean(UserRepository)
                await users.save(User(email="a@example.com"))
                assert await users.count() == 1  # rolled back when the test ends

    Every test of the class runs in a data slice (:func:`~pyfly.testing.slice_context.data_slice`) of *beans*
    that the ``data_context`` fixture of PyFly's pytest plugin builds and hands over, and every unit of work of
    the test rolls back when it ends (*rollback*). The configuration is *config*, or the ``pyfly_data_config``
    fixture's (override it in ``conftest.py`` for a database container); by default a SQLite file in the
    test's ``tmp_path``.
    """
    options = DataTestOptions(
        beans=tuple(beans),
        config=config,
        overrides=dict(overrides or {}),
        rollback=rollback,
        datasources=tuple(datasources) if datasources is not None else None,
    )

    def mark(target: type) -> type:
        setattr(target, _SLICE_MARKER, "data")
        setattr(target, _DATA_TEST, options)
        try:
            import pytest
        except ImportError:  # outside a pytest run the class only carries its options
            return target
        existing = target.__dict__.get("pytestmark", [])
        marks = list(existing) if isinstance(existing, (list, tuple)) else [existing]
        target.pytestmark = [*marks, pytest.mark.usefixtures("data_context")]  # type: ignore[attr-defined]
        return target

    return mark(cls) if cls is not None else mark


def ServiceTest(cls: type) -> type:  # noqa: N802
    """Mark test class as a service-layer test slice (build it with ``service_slice``)."""
    setattr(cls, _SLICE_MARKER, "service")
    return cls


def get_test_slice(cls: type) -> str | None:
    """Get the test slice type for a class, or None if not sliced."""
    return getattr(cls, _SLICE_MARKER, None)


def data_test_options(cls: type | None) -> DataTestOptions | None:
    """The options ``@DataTest`` gave *cls*, or ``None``."""
    options = getattr(cls, _DATA_TEST, None) if cls is not None else None
    return options if isinstance(options, DataTestOptions) else None
