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
"""Temporary feature-flag overrides for tests, with or without a running application."""

from __future__ import annotations

import contextlib
import functools
import inspect
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pyfly.feature_flags.slot import install_feature_flags, installed_feature_flags, uninstall_feature_flags

if TYPE_CHECKING:
    from pyfly.feature_flags.client import FeatureFlags
    from pyfly.feature_flags.registry import FlagRegistry

__all__ = ["TEST_DOMAIN", "FlagOverrides", "override_flags"]

TEST_DOMAIN = "pyfly-test-overrides"

_F = TypeVar("_F", bound=Callable[..., Any])


class override_flags(contextlib.ContextDecorator):  # noqa: N801 — reads as a decorator and context manager
    """Override *flags* while the block or decorated function runs."""

    def __init__(self, flags: Mapping[str, Any]) -> None:
        self._flags = dict(flags)
        self._registry: FlagRegistry | None = None
        self._previous: dict[str, Any] = {}
        self._standalone: FeatureFlags | None = None
        self._replaced: tuple[FeatureFlags, int] | None = None

    def __enter__(self) -> FeatureFlags:
        installed = installed_feature_flags()
        registry = installed.facade.registry if installed is not None else None
        if installed is not None and registry is not None:
            previous = registry.test_overrides
            registry.set_test_overrides({**previous, **self._flags})
            self._registry = registry
            self._previous = previous
            return installed.facade

        from openfeature import api
        from openfeature.client import OpenFeatureClient

        from pyfly.feature_flags.client import FeatureFlags
        from pyfly.feature_flags.context import EvaluationContextResolver
        from pyfly.feature_flags.provider import FireflyFlagProvider
        from pyfly.feature_flags.registry import FlagRegistry

        provider = FireflyFlagProvider()
        standalone = FlagRegistry([], provider)
        standalone.set_test_overrides(self._flags)
        api.set_provider_and_wait(provider, TEST_DOMAIN)
        facade = FeatureFlags(
            OpenFeatureClient(domain=TEST_DOMAIN, version=None), EvaluationContextResolver(), registry=standalone
        )
        status = installed.disabled_status if installed is not None else 404
        self._replaced = (installed.facade, status) if installed is not None else None
        install_feature_flags(facade, disabled_status=status)
        self._standalone = facade
        return facade

    def __exit__(self, *exc: object) -> None:
        if self._registry is not None:
            if self._previous:
                self._registry.set_test_overrides(self._previous)
            else:
                self._registry.clear_test_overrides()
            self._registry = None
        elif self._standalone is not None:
            from openfeature import api
            from openfeature.provider.no_op_provider import NoOpProvider

            uninstall_feature_flags(self._standalone)
            if self._replaced is not None:
                install_feature_flags(self._replaced[0], disabled_status=self._replaced[1])
            api.set_provider_and_wait(NoOpProvider(), TEST_DOMAIN)
            self._standalone = None
            self._replaced = None

    async def __aenter__(self) -> FeatureFlags:
        return self.__enter__()

    async def __aexit__(self, *exc: object) -> None:
        self.__exit__(*exc)

    def __call__(self, function: _F) -> _F:
        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with override_flags(self._flags):
                    return await function(*args, **kwargs)

            return cast(_F, async_wrapper)

        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with override_flags(self._flags):
                return function(*args, **kwargs)

        return cast(_F, wrapper)


class FlagOverrides:
    """A fixture helper whose successive overrides last until reset."""

    def __init__(self) -> None:
        self._active: list[override_flags] = []

    def set(self, flags: Mapping[str, Any]) -> FeatureFlags:
        override = override_flags(flags)
        facade = override.__enter__()
        self._active.append(override)
        return facade

    def reset(self) -> None:
        while self._active:
            self._active.pop().__exit__(None, None, None)
