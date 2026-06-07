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
"""SESSION-scoped beans (v26.06.46): one instance per HttpSession."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pyfly.container.container import Container
from pyfly.container.types import Scope
from pyfly.context.request_context import HTTP_SESSION_KEY, RequestContext
from pyfly.session.session import HttpSession


class SessionBean:
    pass


@pytest.fixture(autouse=True)
def _clean_request_context() -> Iterator[None]:
    RequestContext.clear()
    yield
    RequestContext.clear()


def _container() -> Container:
    container = Container()
    container.register(SessionBean, scope=Scope.SESSION)
    return container


def test_same_instance_within_one_session() -> None:
    container = _container()
    ctx = RequestContext.init()
    ctx.set(HTTP_SESSION_KEY, HttpSession("sid-1", {}))
    assert container.resolve(SessionBean) is container.resolve(SessionBean)


def test_distinct_instances_across_sessions() -> None:
    container = _container()
    ctx = RequestContext.init()
    ctx.set(HTTP_SESSION_KEY, HttpSession("sid-1", {}))
    first = container.resolve(SessionBean)
    ctx.set(HTTP_SESSION_KEY, HttpSession("sid-2", {}))
    second = container.resolve(SessionBean)
    assert first is not second


def test_requires_an_http_session() -> None:
    container = _container()
    RequestContext.init()  # no session attached
    with pytest.raises(RuntimeError, match="No HTTP session"):
        container.resolve(SessionBean)


def test_requires_a_request_context() -> None:
    container = _container()
    with pytest.raises(RuntimeError, match="No active request context"):
        container.resolve(SessionBean)
