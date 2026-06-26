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
"""Auto-configuration that registers dependency-free security WebFilters.

The CSRF filter and the URL-authorization filter live in the Starlette adapter
but were never wired (audit #42/#47). Registering them as ``WebFilter`` beans
lets the post-start filter rescan in ``create_app`` add them to the live chain.
(The JWT ``SecurityFilter`` / OAuth2 resource-server filter are registered by
the security auto-configs that own their ``JWTService`` / ``JWKSTokenValidator``
dependency, to avoid cross-configuration bean ordering issues.) Concrete filter
classes are imported lazily so this module is importable without Starlette.
"""

from __future__ import annotations

from collections.abc import Sequence

from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_bean,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.security.http_security import HttpSecurity
from pyfly.web.ports.filter import WebFilter


def _exclude_patterns(config: Config, key: str) -> Sequence[str]:
    """Read a list / comma-separated string of exclude path patterns from config."""
    raw = config.get(key)
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


@auto_configuration
@conditional_on_class("starlette")
@conditional_on_property("pyfly.security.csrf.enabled", having_value="true", match_if_missing=True)
class CsrfFilterAutoConfiguration:
    """Registers the double-submit-cookie CSRF filter.

    Secure by default: active unless ``pyfly.security.csrf.enabled=false``. The
    filter runs in cookie-gated mode (``pyfly.security.csrf.cookie-gated``,
    default true), so stateless/token (no-cookie) clients are unaffected while
    browser/session requests are protected. Set ``cookie-gated: false`` for
    strict enforcement of every unsafe request.
    """

    @bean
    def csrf_filter(self, config: Config) -> WebFilter:
        from pyfly.web.adapters.starlette.filters.csrf_filter import CsrfFilter

        cookie_gated = str(config.get("pyfly.security.csrf.cookie-gated", True)).strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        filter_ = CsrfFilter(cookie_gated=cookie_gated)
        excludes = _exclude_patterns(config, "pyfly.security.csrf.exclude-patterns")
        if excludes:
            filter_.exclude_patterns = list(excludes)
        return filter_


@auto_configuration
@conditional_on_class("starlette")
@conditional_on_bean(HttpSecurity)
class HttpSecurityFilterAutoConfiguration:
    """Builds the URL-rule authorization filter from a user ``HttpSecurity`` bean."""

    @bean
    def http_security_filter(self, http_security: HttpSecurity) -> WebFilter:
        return http_security.build()
