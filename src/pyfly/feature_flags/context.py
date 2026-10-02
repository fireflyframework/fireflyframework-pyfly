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
"""The ambient evaluation context (spec 4.4) and the per-request OpenFeature transaction context.

Built-in contributors give ``targetingKey`` and ``roles`` (the authenticated principal), ``tenant`` (a principal
attribute, or the ``X-Tenant-Id`` header when trusted) and ``application``/``profiles``. Applications add attributes
with :class:`EvaluationContextContributor` beans, which run after the built-ins in ``@order`` order and may override
them. The resolver finds those beans once the context has refreshed (every singleton exists then); before that it
scans the container on each call.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from openfeature.evaluation_context import EvaluationContext
from openfeature.transaction_context import get_transaction_context, set_transaction_context

from pyfly.container.ordering import HIGHEST_PRECEDENCE, get_order, order
from pyfly.context.events import ContextRefreshedEvent, app_event_listener
from pyfly.observability.correlation import get_tenant_id
from pyfly.security.context_holder import SecurityContextHolder
from pyfly.web.filters import OncePerRequestFilter

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.web.ports.filter import CallNext

__all__ = [
    "TARGETING_KEY",
    "ApplicationContextContributor",
    "EvaluationContextContributor",
    "EvaluationContextResolver",
    "FeatureFlagsContextFilter",
    "SecurityContextContributor",
    "TenantContextContributor",
]

_logger = logging.getLogger(__name__)

TARGETING_KEY = "targetingKey"


@runtime_checkable
class EvaluationContextContributor(Protocol):
    """Adds (or overrides) attributes of the ambient evaluation context, in place."""

    def contribute(self, attributes: dict[str, Any]) -> None: ...


class SecurityContextContributor:
    """``targetingKey`` (the principal's ``user_id``) and ``roles`` (without ``ROLE_``), when authenticated."""

    def contribute(self, attributes: dict[str, Any]) -> None:
        context = SecurityContextHolder.get_context()
        if context is None or not context.is_authenticated:
            return
        attributes[TARGETING_KEY] = context.user_id
        attributes["roles"] = [role.removeprefix("ROLE_") for role in context.roles]


class TenantContextContributor:
    """``tenant``: the principal attribute *tenant_attribute*, else (when trusted) the ``X-Tenant-Id`` header."""

    def __init__(self, *, tenant_attribute: str = "tenant", trust_tenant_header: bool = False) -> None:
        self._attribute = tenant_attribute
        self._trust_header = trust_tenant_header

    def contribute(self, attributes: dict[str, Any]) -> None:
        context = SecurityContextHolder.get_context()
        tenant = context.attributes.get(self._attribute) if context is not None and context.is_authenticated else None
        if not tenant and self._trust_header:
            tenant = get_tenant_id()
        if tenant:
            attributes["tenant"] = str(tenant)


class ApplicationContextContributor:
    """``application`` (``pyfly.app.name``) and ``profiles`` (the active profiles)."""

    def __init__(self, *, application: str, profiles: Sequence[str]) -> None:
        self._application = application
        self._profiles = list(profiles)

    def contribute(self, attributes: dict[str, Any]) -> None:
        attributes["application"] = self._application
        attributes["profiles"] = list(self._profiles)


class EvaluationContextResolver:
    """Builds the ambient context: the built-ins, then the application's contributors."""

    def __init__(
        self,
        builtins: Sequence[EvaluationContextContributor] = (),
        *,
        container: Container | None = None,
        contributors: Sequence[EvaluationContextContributor] | None = None,
    ) -> None:
        self._builtins = list(builtins)
        self._container = container
        self._explicit = list(contributors) if contributors is not None else None
        self._found: list[EvaluationContextContributor] | None = None

    @app_event_listener
    async def on_context_refreshed(self, event: ContextRefreshedEvent) -> None:
        """Every singleton exists now: find the contributor beans once."""
        self._found = self._scan()

    def _scan(self) -> list[EvaluationContextContributor]:
        if self._container is None:
            return []
        builtin_ids = {id(contributor) for contributor in self._builtins}
        found: dict[int, EvaluationContextContributor] = {}
        for cls in self._container.registered_types():
            registration = self._container.get_registration(cls)
            instance = registration.instance if registration is not None else None
            if (
                isinstance(instance, EvaluationContextContributor)
                and id(instance) not in builtin_ids
                and id(instance) not in found
            ):
                found[id(instance)] = instance
        return sorted(found.values(), key=lambda contributor: get_order(type(contributor)))

    def _contributors(self) -> list[EvaluationContextContributor]:
        if self._explicit is not None:
            return [*self._builtins, *self._explicit]
        user = self._found if self._found is not None else self._scan()
        return [*self._builtins, *user]

    def attributes(self) -> dict[str, Any]:
        """The ambient attributes: the built-ins, then the application's contributors (a broken one is skipped)."""
        attributes: dict[str, Any] = {}
        for contributor in self._contributors():
            try:
                contributor.contribute(attributes)
            except Exception:  # noqa: BLE001 — a contributor never breaks an evaluation
                _logger.debug(
                    "feature_flag_context_contributor_failed",
                    extra={"contributor": type(contributor).__name__},
                    exc_info=True,
                )
        return attributes

    def resolve(self) -> EvaluationContext:
        """The ambient context, with ``targetingKey`` moved into ``EvaluationContext.targeting_key``."""
        attributes = self.attributes()
        key = attributes.pop(TARGETING_KEY, None)
        return EvaluationContext(targeting_key=str(key) if key not in (None, "") else None, attributes=attributes)

    def process_attributes(self) -> dict[str, Any]:
        """The attributes of the process, not of the caller: the management preview evaluates with these."""
        attributes: dict[str, Any] = {}
        for contributor in self._builtins:
            if isinstance(contributor, ApplicationContextContributor):
                contributor.contribute(attributes)
        return attributes


@order(HIGHEST_PRECEDENCE + 400)
class FeatureFlagsContextFilter(OncePerRequestFilter):
    """Sets the OpenFeature transaction context to the request's ambient context, so third-party OpenFeature
    clients see it too. Ordered after every security filter (``HttpSecurityFilter`` is ``HIGHEST_PRECEDENCE + 350``)
    and restores the previous transaction context when the request ends."""

    def __init__(self, resolver: EvaluationContextResolver) -> None:
        self._resolver = resolver

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        previous = get_transaction_context()
        set_transaction_context(self._resolver.resolve())
        try:
            return await call_next(request)
        finally:
            set_transaction_context(previous)
