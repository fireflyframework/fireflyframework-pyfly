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
"""Autowired descriptor for field-level dependency injection."""

from __future__ import annotations


class Autowired:
    """Marks a class attribute for field injection by the DI container.

    Usage::

        @service
        class OrderService:
            repo: OrderRepository = Autowired()
            cache: CacheAdapter = Autowired(qualifier="redis_cache")
            metrics: MetricsCollector = Autowired(required=False)

    After the container creates an instance via constructor injection, it
    inspects class annotations for ``Autowired`` sentinels and injects the
    resolved dependency via ``setattr``.

    Args:
        qualifier: If set, resolve by bean name instead of type.
        required: If ``False``, unresolvable dependencies are set to ``None``
            instead of raising ``NoSuchBeanError``. Defaults to ``True``.
    """

    __slots__ = ("qualifier", "required")

    def __init__(self, *, qualifier: str | None = None, required: bool = True) -> None:
        self.qualifier = qualifier
        self.required = required

    def __repr__(self) -> str:
        parts: list[str] = []
        if self.qualifier:
            parts.append(f"qualifier={self.qualifier!r}")
        if not self.required:
            parts.append("required=False")
        return f"Autowired({', '.join(parts)})"
