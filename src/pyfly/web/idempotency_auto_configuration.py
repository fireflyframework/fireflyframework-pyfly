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
"""Auto-configuration for the HTTP idempotency filter.

The :class:`~pyfly.web.adapters.starlette.filters.idempotency_filter.IdempotencyWebFilter`
is registered as a ``WebFilter`` bean only when **both** conditions are met:

1. ``pyfly.web.idempotency.enabled`` is ``true`` (opt-in, default ``false``).
2. Starlette is importable on the class-path.

A :class:`~pyfly.cache.ports.outbound.CacheAdapter` bean is **optionally**
injected.  When the feature is enabled but no cache adapter is present an
explicit :class:`RuntimeError` is raised with a descriptive message so the
operator gets actionable feedback.

TTL defaults to 86 400 s (24 h) and is overridable via
``pyfly.web.idempotency.ttl-seconds``.
"""

from __future__ import annotations

from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.web.ports.filter import WebFilter


@auto_configuration
@conditional_on_class("starlette")
@conditional_on_property("pyfly.web.idempotency.enabled", having_value="true")
class IdempotencyFilterAutoConfiguration:
    """Registers the HTTP idempotency ``WebFilter`` bean (opt-in).

    Enable via ``pyfly.web.idempotency.enabled: true``.  A
    ``CacheAdapter`` bean must be present at startup; if not, startup fails
    with a descriptive error rather than silently disabling caching.
    """

    @bean
    def idempotency_web_filter(
        self,
        config: Config,
        cache: CacheAdapter | None = None,
    ) -> WebFilter:
        from pyfly.web.adapters.starlette.filters.idempotency_filter import (
            IdempotencyWebFilter,
        )

        if cache is None:
            raise RuntimeError(
                "pyfly.web.idempotency.enabled is true but no CacheAdapter bean is "
                "registered.  Configure a cache backend "
                "(e.g. pyfly.cache.enabled: true) before enabling the idempotency filter."
            )

        ttl_seconds = int(config.get("pyfly.web.idempotency.ttl-seconds", 86400))
        return IdempotencyWebFilter(cache=cache, ttl_seconds=ttl_seconds)
