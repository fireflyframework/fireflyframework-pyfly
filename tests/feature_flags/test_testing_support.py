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
"""Flag overrides in tests, with or without a running application."""

from __future__ import annotations

import pytest

import pyfly.testing
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.client import FeatureFlags
from pyfly.feature_flags.definitions import FlagDefinitionError
from pyfly.feature_flags.gating import FeatureFlagDisabledException, feature_flag
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.slot import installed_feature_flags
from pyfly.testing.feature_flags import FlagOverrides, override_flags


@feature_flag("new-checkout")
def checkout() -> str:
    return "new"


@feature_flag("new-checkout")
async def checkout_async() -> str:
    return "new"


def test_without_an_application_the_overrides_install_a_provider_of_their_own() -> None:
    with pytest.raises(FeatureFlagDisabledException):
        checkout()
    with override_flags({"new-checkout": True, "theme": "dark"}) as flags:
        assert checkout() == "new"
        assert flags.get_string("theme", "light") == "dark"
    assert installed_feature_flags() is None
    with pytest.raises(FeatureFlagDisabledException):
        checkout()


@override_flags({"new-checkout": True})
def test_the_decorator_form_on_a_sync_test() -> None:
    assert checkout() == "new"


@override_flags({"new-checkout": True})
async def test_the_decorator_form_on_an_async_test() -> None:
    assert await checkout_async() == "new"


async def test_async_with_works_too() -> None:
    async with override_flags({"new-checkout": True}):
        assert await checkout_async() == "new"


def test_overrides_nest_and_unwind() -> None:
    with override_flags({"new-checkout": True}) as outer:
        with override_flags({"theme": "dark"}) as inner:
            assert inner.is_enabled("new-checkout") is True and inner.get_string("theme", "x") == "dark"
        assert outer.is_enabled("new-checkout") is True
        assert outer.get_string("theme", "x") == "x"


async def test_with_a_running_application_the_overrides_are_its_highest_layer() -> None:
    config = Config({"pyfly": {"feature-flags": {"enabled": "true", "flags": {"new-checkout": False}}}})
    context = ApplicationContext(config)
    await context.start()
    try:
        app_flags = context.get_bean(FeatureFlags)
        registry = context.get_bean(FlagRegistry)
        with override_flags({"new-checkout": True}) as flags:
            assert flags is app_flags
            assert app_flags.is_enabled("new-checkout") is True
            assert registry.effective_flag("new-checkout").origin == "test-overrides"  # type: ignore[union-attr]
            assert checkout() == "new"
        assert app_flags.is_enabled("new-checkout") is False
        assert registry.test_overrides == {}
    finally:
        await context.stop()


def test_an_invalid_override_is_refused() -> None:
    with pytest.raises(FlagDefinitionError, match="invalid flag key"), override_flags({"bad key": True}):
        pass


def test_the_fixture_overrides_for_one_test(feature_flags: FlagOverrides) -> None:
    feature_flags.set({"new-checkout": True})
    assert checkout() == "new"
    feature_flags.set({"theme": "dark"})
    assert checkout() == "new"


def test_the_fixture_reset_after_the_previous_test() -> None:
    with pytest.raises(FeatureFlagDisabledException):
        checkout()


def test_the_names_are_exported_by_pyfly_testing() -> None:
    assert pyfly.testing.override_flags is override_flags
    assert pyfly.testing.FlagOverrides is FlagOverrides
    assert {"override_flags", "FlagOverrides"} <= set(pyfly.testing.__all__)
