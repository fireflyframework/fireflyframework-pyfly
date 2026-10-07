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
"""An application booted the canonical way (create_app, then the context started in the lifespan) uses flags from
configuration and a watched file, with the ambient context, change events, exposure events and the metric."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from openfeature import api
from prometheus_client import REGISTRY

from pyfly.container import component, rest_controller
from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import app_event_listener
from pyfly.core.config import Config
from pyfly.feature_flags.client import FeatureFlags
from pyfly.feature_flags.events import FeatureFlagEvaluated, FeatureFlagsChanged
from pyfly.feature_flags.hooks import EVALUATIONS_METRIC
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.slot import installed_feature_flags
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder
from pyfly.web import get_mapping, request_mapping
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext
from tests.feature_flags.support import bool_flag, wait_until

NEW_CHECKOUT = {**bool_flag("off"), "targeting": {"if": [{"in": ["beta", {"var": "roles"}]}, "on", None]}}

# A flag whose variant is plain text that happens to look like a configuration placeholder.
BANNER_TEXT = "Welcome back, ${user}!"
BANNER = {"state": "ENABLED", "variants": {"welcome": BANNER_TEXT}, "defaultVariant": "welcome"}

# Both date-times are 2025-01-01T04:30:00Z, which the epoch-ms contract evaluates as 1735705800000: the naive one is
# read as UTC (never in the host's zone) and the aware one honors its offset.
SIGNED_UP = datetime(2025, 1, 1, 4, 30)
LAST_SEEN = datetime(2024, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
INSTANT_EPOCH_MS = 1735705800000
SIGNED_UP_FLAG = {
    **bool_flag("off"),
    "targeting": {
        "if": [
            {
                "and": [
                    {"==": [{"var": "signedUp"}, INSTANT_EPOCH_MS]},
                    {"==": [{"var": "lastSeen"}, INSTANT_EPOCH_MS]},
                ]
            },
            "on",
            None,
        ]
    },
}


def _document(theme_default: str) -> dict[str, Any]:
    """The flag file: ``theme`` is the flag the test flips; ``signed-up`` reads two date-time attributes."""
    return {
        "flags": {
            "theme": {
                "state": "ENABLED",
                "variants": {"light": "light", "dark": "dark"},
                "defaultVariant": theme_default,
            },
            "signed-up": SIGNED_UP_FLAG,
        }
    }


@component
@order(HIGHEST_PRECEDENCE + 220)
class HeaderAuthentication(OncePerRequestFilter):
    """Stands in for the security filters: X-User and X-Roles authenticate the request."""

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        user = request.headers.get("x-user")
        if user is None:
            return await call_next(request)
        roles = [f"ROLE_{role}" for role in request.headers.get("x-roles", "").split(",") if role]
        token = SecurityContextHolder.set_context(SecurityContext(user_id=user, roles=roles))
        try:
            return await call_next(request)
        finally:
            SecurityContextHolder.reset_context(token)


@component
class SignupContributor:
    """An application contributor: an authenticated principal carries date-time attributes."""

    def contribute(self, attributes: dict[str, Any]) -> None:
        if "targetingKey" in attributes:
            attributes["signedUp"] = SIGNED_UP
            attributes["lastSeen"] = LAST_SEEN


@component
class FlagEvents:
    def __init__(self) -> None:
        self.changes: list[FeatureFlagsChanged] = []
        self.evaluations: list[FeatureFlagEvaluated] = []

    @app_event_listener
    async def on_change(self, event: FeatureFlagsChanged) -> None:
        self.changes.append(event)

    @app_event_listener
    async def on_evaluation(self, event: FeatureFlagEvaluated) -> None:
        self.evaluations.append(event)


@rest_controller
@request_mapping("/shop")
class ShopController:
    def __init__(self, flags: FeatureFlags) -> None:
        self._flags = flags

    @get_mapping("/checkout")
    async def checkout(self) -> dict[str, Any]:
        third_party = api.get_client()  # no Firefly hooks here
        return {
            "newCheckout": await self._flags.is_enabled_async("new-checkout"),
            "theme": self._flags.get_string("theme", "unknown"),
            "banner": self._flags.get_string("banner", "unknown"),
            "signedUp": self._flags.is_enabled("signed-up"),
            "thirdParty": third_party.get_boolean_value("new-checkout", False),
            "thirdPartySignedUp": third_party.get_boolean_value("signed-up", False),
        }


def _application(flags_file: Path) -> tuple[ApplicationContext, Any]:
    config = Config(
        {
            "pyfly": {
                "app": {"name": "shop"},
                "feature-flags": {
                    "enabled": "true",
                    "flags": {"new-checkout": NEW_CHECKOUT, "banner": BANNER},
                    "sources": {"file": {"enabled": "true", "path": str(flags_file), "refresh-interval": "50ms"}},
                    "events": {"evaluations": "true"},
                },
            }
        }
    )
    context = ApplicationContext(config)
    for bean_class in (HeaderAuthentication, SignupContributor, FlagEvents, ShopController):
        context.register_bean(bean_class)

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    return context, create_app(context=context, lifespan=lifespan, actuator_enabled=False, docs_enabled=False)


async def test_an_application_evaluates_with_the_ambient_context_and_follows_its_flag_file(tmp_path: Path) -> None:
    flags_file = tmp_path / "flags.json"
    flags_file.write_text(json.dumps(_document("light")), encoding="utf-8")
    context, app = _application(flags_file)
    labels = {"flag": "new-checkout", "variant": "on", "reason": "TARGETING_MATCH"}
    counted = REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) or 0.0
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            beta = (await client.get("/shop/checkout", headers={"x-user": "ana", "x-roles": "beta"})).json()
            anonymous = (await client.get("/shop/checkout")).json()
            # a config flag (new-checkout) and a file flag (theme), the ambient roles, the ambient date-times (epoch ms)
            # and the literal text of a ``${...}`` variant, through the facade and through a plain OpenFeature client
            assert beta == {
                "newCheckout": True,
                "theme": "light",
                "banner": BANNER_TEXT,
                "signedUp": True,
                "thirdParty": True,
                "thirdPartySignedUp": True,
            }
            assert anonymous == {
                "newCheckout": False,
                "theme": "light",
                "banner": BANNER_TEXT,
                "signedUp": False,
                "thirdParty": False,
                "thirdPartySignedUp": False,
            }

            events = context.get_bean(FlagEvents)
            flags_file.write_text(json.dumps(_document("dark")), encoding="utf-8")
            stat = flags_file.stat()
            os.utime(flags_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            await wait_until(lambda: any(change.origin == "file" for change in events.changes))
            # one change, for the one flag that differs (signed-up is in the file too, and did not change)
            assert [change.changed_keys for change in events.changes if change.origin == "file"] == [("theme",)]
            assert (await client.get("/shop/checkout")).json()["theme"] == "dark"

            # four evaluations through the facade per request, three requests; the plain client's are not hooked
            await wait_until(lambda: len(events.evaluations) >= 12)
            assert {event.key for event in events.evaluations} == {"new-checkout", "theme", "banner", "signed-up"}
            assert [e.targeting_key for e in events.evaluations if e.key == "new-checkout"][:2] == ["ana", None]
        assert REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) == counted + 1
        registry = context.get_bean(FlagRegistry)
    assert not registry.started
    assert installed_feature_flags() is None
    assert api.get_provider_metadata().name == "No-op Provider"
