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
"""Spring-style stereotype decorators for bean classification.

Each stereotype is an alias for container registration with semantic meaning:
- @component: generic managed bean
- @service: business logic layer
- @repository: data access layer
- @controller: web controller (template responses)
- @rest_controller: REST controller (JSON responses)
- @configuration: config class containing @bean methods
- @shell_component: shell command provider
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

from pyfly.container.types import Scope

T = TypeVar("T", bound=type)


def _make_stereotype(stereotype_name: str) -> Callable[..., Any]:
    """Factory that creates a stereotype decorator with the given name."""

    @overload
    def stereotype(cls: T) -> T: ...

    @overload
    def stereotype(
        *,
        name: str = "",
        scope: Scope = Scope.SINGLETON,
        profile: str = "",
        condition: Callable[..., bool] | None = None,
    ) -> Callable[[T], T]: ...

    def stereotype(
        cls: T | None = None,
        *,
        name: str = "",
        scope: Scope = Scope.SINGLETON,
        profile: str = "",
        condition: Callable[..., bool] | None = None,
    ) -> T | Callable[[T], T]:
        def decorator(cls: T) -> T:
            cls.__pyfly_injectable__ = True  # type: ignore[attr-defined]
            cls.__pyfly_stereotype__ = stereotype_name  # type: ignore[attr-defined]
            cls.__pyfly_scope__ = scope  # type: ignore[attr-defined]
            cls.__pyfly_condition__ = condition  # type: ignore[attr-defined]
            if name:
                cls.__pyfly_bean_name__ = name  # type: ignore[attr-defined]
            if profile:
                cls.__pyfly_profile__ = profile  # type: ignore[attr-defined]
            return cls

        if cls is not None:
            return decorator(cls)
        return decorator

    stereotype.__name__ = stereotype_name
    stereotype.__qualname__ = stereotype_name
    return stereotype


component = _make_stereotype("component")
service = _make_stereotype("service")
repository = _make_stereotype("repository")
controller = _make_stereotype("controller")
rest_controller = _make_stereotype("rest_controller")
configuration = _make_stereotype("configuration")
shell_component = _make_stereotype("shell_component")
controller_advice = _make_stereotype("controller_advice")
