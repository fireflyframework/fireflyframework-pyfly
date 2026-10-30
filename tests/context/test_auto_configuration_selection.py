"""Explicit headless contexts discover only their selected framework subsystems."""

import pytest

from pyfly.container import bean
from pyfly.context.application_context import ApplicationContext
from pyfly.context.conditions import auto_configuration
from pyfly.core.config import Config


async def test_explicit_auto_configurations_do_not_discover_unrelated_entry_points(monkeypatch):
    class SelectedValue:
        pass

    @auto_configuration
    class SelectedConfiguration:
        @bean
        def selected_value(self) -> SelectedValue:
            return SelectedValue()

    def reject_discovery():
        raise AssertionError("Explicit selection must not discover installed integrations")

    monkeypatch.setattr("pyfly.config.auto.discover_auto_configurations", reject_discovery)
    context = ApplicationContext(Config({}), auto_configurations=[SelectedConfiguration])
    await context.start()
    try:
        assert isinstance(context.get_bean(SelectedValue), SelectedValue)
    finally:
        await context.stop()


async def test_empty_auto_configuration_selection_is_a_valid_plain_context(monkeypatch):
    monkeypatch.setattr("pyfly.config.auto.discover_auto_configurations", lambda: pytest.fail("Unexpected discovery"))
    context = ApplicationContext(Config({}), auto_configurations=[])
    await context.start()
    assert context.get_bean(Config).get("unconfigured") is None
    await context.stop()
