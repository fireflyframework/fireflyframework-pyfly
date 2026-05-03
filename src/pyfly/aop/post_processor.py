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
"""AspectBeanPostProcessor — automatically weaves AOP advice into beans."""

from __future__ import annotations

from typing import Any

from pyfly.aop.registry import AspectRegistry
from pyfly.aop.weaver import weave_bean


class AspectBeanPostProcessor:
    """BeanPostProcessor that collects @aspect beans and weaves advice.

    During ``before_init``, aspect beans are collected into an internal
    :class:`AspectRegistry`.  During ``after_init``, non-aspect beans
    have their public methods wrapped with matching advice chains.
    """

    def __init__(self) -> None:
        self._registry: AspectRegistry | None = None
        self._aspect_types: set[type] = set()

    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Collect @aspect beans into the registry."""
        if getattr(type(bean), "__pyfly_aspect__", False):
            if self._registry is None:
                self._registry = AspectRegistry()
            self._registry.register(bean)
            self._aspect_types.add(type(bean))
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Weave advice into non-aspect beans."""
        if self._registry is None:
            return bean
        if type(bean) in self._aspect_types:
            return bean

        stereotype = getattr(type(bean), "__pyfly_stereotype__", "")
        if stereotype:
            prefix = f"{stereotype}.{type(bean).__name__}"
        else:
            prefix = f"{type(bean).__module__}.{type(bean).__name__}"

        weave_bean(bean, prefix, self._registry)
        return bean
