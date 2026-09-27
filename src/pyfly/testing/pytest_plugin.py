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
"""PyFly's pytest plugin: data tests whose units of work roll back (``@DataTest``).

pytest loads it with PyFly (the ``pyfly`` pytest11 entry point). It adds:

- the ``data_context`` fixture: a started data slice (:func:`~pyfly.testing.slice_context.data_slice`) for the
  test, with the beans and options of its ``@DataTest`` class (:func:`~pyfly.testing.slices.DataTest`) or of
  its ``@pytest.mark.data_test(beans=[...], ...)`` marker, whose units of work roll back when the test ends.
  Every test of a ``@DataTest`` class uses it;
- the ``pyfly_data_config`` fixture: the configuration of those slices when the test names none. Override it
  in ``conftest.py`` to run them on a database container::

      @pytest.fixture
      def pyfly_data_config(postgres):  # a started testcontainers PostgresContainer
          return pyfly_config(postgres)

  By default it is a SQLite file in the test's ``tmp_path`` with the relational layer enabled;
- the ``data_test`` marker.

The fixtures are asynchronous: run the tests with pytest-asyncio (``asyncio_mode = "auto"``, as the projects
``pyfly new`` generates do) or declare them ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import pytest

from pyfly.testing.slices import DataTestOptions, data_test_options

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config

_F = TypeVar("_F", bound=Callable[..., Any])

try:
    import pytest_asyncio

    _fixture: Any = pytest_asyncio.fixture
except ImportError:  # pragma: no cover — without pytest-asyncio the fixtures need another async runner
    _fixture = pytest.fixture


def _async_fixture(function: _F) -> _F:
    """An asynchronous fixture: pytest-asyncio's (strict and auto mode alike) when it is installed."""
    return cast(_F, _fixture(function))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "data_test(beans=(), config=None, overrides=None, rollback=True, datasources=None): run the test in a "
        "PyFly data slice (the data_context fixture) whose units of work roll back when it ends",
    )


def _options(request: pytest.FixtureRequest) -> DataTestOptions:
    marker = request.node.get_closest_marker("data_test")
    if marker is not None:
        if marker.args:
            raise pytest.UsageError("@pytest.mark.data_test takes keyword arguments only: data_test(beans=[...])")
        kwargs = dict(marker.kwargs)
        beans = tuple(kwargs.pop("beans", ()))
        datasources = kwargs.pop("datasources", None)
        return DataTestOptions(
            beans=beans,
            datasources=tuple(datasources) if datasources is not None else None,
            overrides=dict(kwargs.pop("overrides", None) or {}),
            **kwargs,
        )
    return data_test_options(getattr(request, "cls", None)) or DataTestOptions()


def default_data_config(directory: Path) -> Config:
    """The configuration of a data test that names none: the relational layer on a SQLite file in
    *directory* (a file, not ``:memory:``: an in-memory database lives on one connection that every session
    shares, so it cannot show what a transaction does)."""
    from pyfly.core.config import Config

    url = f"sqlite+aiosqlite:///{directory / 'pyfly-data-test.db'}"
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url}}}})


@pytest.fixture
def pyfly_data_config() -> Config | None:
    """The configuration of ``@DataTest`` slices that name none (``None``: a SQLite file in ``tmp_path``)."""
    return None


@_async_fixture
async def data_context(
    request: pytest.FixtureRequest, tmp_path: Path, pyfly_data_config: Config | None
) -> AsyncIterator[ApplicationContext]:
    """A started data slice for the test (see the module documentation); its units of work roll back when the
    test ends, and the slice stops."""
    from pyfly.testing.slice_context import data_slice

    options = _options(request)
    config = options.config or pyfly_data_config or default_data_config(tmp_path)
    async with await data_slice(
        *options.beans,
        config=config,
        overrides=dict(options.overrides),
        rollback=options.rollback,
        datasources=options.datasources,
    ) as context:
        yield context
