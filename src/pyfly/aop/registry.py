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
"""AspectRegistry — collects and queries advice bindings for AOP weaving."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from pyfly.aop.pointcut import matches_pointcut
from pyfly.container.ordering import get_order


@dataclass
class AdviceBinding:
    """A single piece of advice bound to a pointcut.

    Attributes:
        advice_type: One of ``"before"``, ``"after_returning"``,
            ``"after_throwing"``, ``"after"``, ``"around"``.
        pointcut: The pointcut expression string.
        handler: The bound method on the aspect instance that implements
            the advice logic.
        aspect_order: Numeric ordering value from :func:`get_order`.
    """

    advice_type: str
    pointcut: str
    handler: Any
    aspect_order: int


class AspectRegistry:
    """Registry that collects aspect instances and provides advice lookups.

    Usage::

        registry = AspectRegistry()
        registry.register(logging_aspect_instance)
        registry.register(security_aspect_instance)

        # Get all bindings that match a qualified name
        bindings = registry.get_matching("service.OrderService.create")
    """

    def __init__(self) -> None:
        self._bindings: list[AdviceBinding] = []

    def register(self, aspect_instance: Any) -> None:
        """Extract advice methods from *aspect_instance* and store bindings.

        A method is considered an advice method if it has the
        ``__pyfly_advice_type__`` attribute (set by the advice decorators).

        Bindings are kept sorted by ``aspect_order`` after each registration.
        """
        aspect_cls = type(aspect_instance)
        order = get_order(aspect_cls)

        for name, method in inspect.getmembers(aspect_instance, predicate=inspect.ismethod):
            advice_type = getattr(method, "__pyfly_advice_type__", None)
            if advice_type is None:
                # Also check the underlying function (unbound) for the attribute,
                # because bound methods may not propagate all attributes.
                unbound = getattr(aspect_cls, name, None)
                if unbound is not None:
                    advice_type = getattr(unbound, "__pyfly_advice_type__", None)
            if advice_type is None:
                continue

            pointcut = getattr(method, "__pyfly_pointcut__", None)
            if pointcut is None:
                unbound = getattr(aspect_cls, name, None)
                if unbound is not None:
                    pointcut = getattr(unbound, "__pyfly_pointcut__", None)
            if pointcut is None:
                continue

            self._bindings.append(
                AdviceBinding(
                    advice_type=advice_type,
                    pointcut=pointcut,
                    handler=method,
                    aspect_order=order,
                )
            )

        # Keep bindings globally sorted by order.
        self._bindings.sort(key=lambda b: b.aspect_order)

    def get_all_bindings(self) -> list[AdviceBinding]:
        """Return all registered bindings, sorted by ``aspect_order``."""
        return list(self._bindings)

    def get_matching(self, qualified_name: str) -> list[AdviceBinding]:
        """Return bindings whose pointcut matches *qualified_name*."""
        return [b for b in self._bindings if matches_pointcut(b.pointcut, qualified_name)]
