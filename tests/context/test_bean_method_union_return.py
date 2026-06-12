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
"""Regression: a ``@bean`` factory with a PEP 604 union return type (``Foo | None``)
must not poison the container or the admin introspection endpoints.

``typing.get_type_hints`` preserves ``Foo | None`` as a ``types.UnionType`` (it is
*not* normalized to ``Optional``), and the configuration processor used to register
that union object directly as a ``_registrations`` key. A ``types.UnionType`` has no
``__name__``/``__qualname__``/``__module__``, so the admin BeansProvider — which
derives names/types from the registration key — crashed with ``AttributeError``,
surfacing as a 500 on ``/admin/api/beans/graph`` (and ``/beans``, ``/beans/{name}``).

``Foo | None`` is the most idiomatic optional-bean signature in Python, so this
broke the admin dashboard for ordinary apps.
"""

from __future__ import annotations

import json

import pytest

from pyfly.admin.providers.beans_provider import BeansProvider
from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


class Widget:
    """A plain bean produced by a union-returning factory."""


@configuration
class WidgetConfig:
    @bean
    def make_widget(self) -> Widget | None:
        return Widget()


class _ProviderCtx:
    """Minimal context exposing the real container for BeansProvider."""

    def __init__(self, container: object) -> None:
        self.container = container


@pytest.mark.asyncio
async def test_union_return_does_not_register_union_key() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(WidgetConfig)
    await ctx.start()

    # No non-class object (e.g. a types.UnionType) may become a registration key.
    for key in ctx.container._registrations:
        assert isinstance(key, type), f"non-class registration key leaked: {key!r}"


@pytest.mark.asyncio
async def test_union_return_bean_still_resolves() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(WidgetConfig)
    await ctx.start()

    # The concrete bean is still resolvable by its real type.
    assert isinstance(ctx.get_bean(Widget), Widget)


@pytest.mark.asyncio
async def test_admin_endpoints_survive_union_return_bean() -> None:
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(WidgetConfig)
    await ctx.start()

    provider = BeansProvider(_ProviderCtx(ctx.container))  # type: ignore[arg-type]

    # Each admin endpoint must return a JSON-serialisable payload (no 500).
    graph = await provider.get_bean_graph()
    json.dumps(graph)

    beans = await provider.get_beans()
    json.dumps(beans)

    for node in graph["nodes"]:
        detail = await provider.get_bean_detail(node["name"])
        json.dumps(detail)
