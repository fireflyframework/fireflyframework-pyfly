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
"""Regression: two @bean methods returning the SAME concrete type must both
survive — previously the second overwrote the first in the type-keyed registry
and `list[T]` resolution returned nothing (silent data loss)."""

from __future__ import annotations

import pytest

from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


class Widget:
    def __init__(self, label: str) -> None:
        self.label = label


@configuration
class TwoWidgetsConfig:
    @bean
    def widget_one(self) -> Widget:
        return Widget("one")

    @bean
    def widget_two(self) -> Widget:
        return Widget("two")


@service
class WidgetConsumer:
    def __init__(self, widgets: list[Widget]) -> None:
        self.widgets = widgets


class TestSameTypeBeans:
    @pytest.mark.asyncio
    async def test_both_named_beans_resolvable(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(TwoWidgetsConfig)
        await ctx.start()
        assert ctx.get_bean_by_name("widget_one").label == "one"
        assert ctx.get_bean_by_name("widget_two").label == "two"

    @pytest.mark.asyncio
    async def test_resolve_all_returns_every_same_type_bean(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(TwoWidgetsConfig)
        await ctx.start()
        labels = sorted(w.label for w in ctx.container.resolve_all(Widget))
        assert labels == ["one", "two"]

    @pytest.mark.asyncio
    async def test_list_injection_receives_all_same_type_beans(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(TwoWidgetsConfig)
        ctx.register_bean(WidgetConsumer)
        await ctx.start()
        consumer = ctx.get_bean(WidgetConsumer)
        assert sorted(w.label for w in consumer.widgets) == ["one", "two"]


class TestSeveralPrimaries:
    def test_two_primary_beans_of_one_type_are_reported_as_two_primaries(self):
        """Two ``@primary`` candidates used to be reported as "none is marked @primary"."""
        from pyfly.container.container import Container
        from pyfly.container.exceptions import NoUniqueBeanError

        container = Container()
        container.register(Widget, name="widget_one")
        container.get_registration(Widget).primary = True  # type: ignore[union-attr]
        container.register(Widget, name="widget_two")
        container.get_registration(Widget).primary = True  # type: ignore[union-attr]

        with pytest.raises(NoUniqueBeanError) as raised:
            container.resolve(Widget)

        message = str(raised.value)
        assert "are marked @primary" in message
        assert "none is marked @primary" not in message
        assert raised.value.primary_names == ["widget_one", "widget_two"]
        assert "Keep @primary on one" in message
