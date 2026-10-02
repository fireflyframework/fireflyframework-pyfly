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
"""``FeatureFlags``, the application's facade, and ``OpenFeatureBinding``, which installs the provider.

The facade evaluates through the framework's OpenFeature client (so its hooks run) with the ambient context from
:class:`~pyfly.feature_flags.context.EvaluationContextResolver` and the caller's explicit context on top. The
binding is a lifecycle bean: it installs the provider (Firefly's, or the application's own) into the global
OpenFeature API, the context-variable transaction-context propagator and the gating slot, and on stop removes each
one only if it is still its own.

The SDK merges the OpenFeature transaction context (which
:class:`~pyfly.feature_flags.context.FeatureFlagsContextFilter` sets to the request's ambient context) underneath
every evaluation. An evaluation that is not *ambient* (the management preview) must not see the caller, so the
facade runs it under an empty transaction context and puts the previous one back afterwards.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import openfeature.transaction_context as _transaction_context
from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, FlagEvaluationOptions, FlagValueType
from openfeature.provider import AbstractProvider, FeatureProvider
from openfeature.provider.no_op_provider import NoOpProvider
from openfeature.transaction_context import (
    ContextVarsTransactionContextPropagator,
    TransactionContextPropagator,
    get_transaction_context,
    set_transaction_context,
    set_transaction_context_propagator,
)

from pyfly.feature_flags.definitions import flag_type
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.slot import install_feature_flags, uninstall_feature_flags

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.feature_flags.context import EvaluationContextResolver
    from pyfly.feature_flags.registry import FlagRegistry

__all__ = [
    "FIREFLY_CLIENT_DOMAIN",
    "PREVIEW_HINT",
    "FeatureFlags",
    "OpenFeatureBinding",
    "client_domain",
    "find_external_provider",
    "typed_default",
]

_logger = logging.getLogger(__name__)

FIREFLY_CLIENT_DOMAIN = "firefly"
"""The domain of the framework's client when ``openfeature.domain`` is empty (it resolves to the default provider)."""

PREVIEW_HINT = "pyfly.preview"
"""Hook hint of a management preview: the Firefly hooks record neither a metric nor an exposure event for it."""


def client_domain(configured: str) -> str:
    """The domain of the framework's client: *configured* (``openfeature.domain``), or ``firefly`` when empty."""
    return configured or FIREFLY_CLIENT_DOMAIN


def typed_default(definition: Mapping[str, Any]) -> FlagValueType:
    """A caller default of the flag's own type: ``False``, ``""``, ``0`` (all-int variants), ``0.0`` or ``{}``."""
    kind = flag_type(definition)
    if kind == "boolean":
        return False
    if kind == "string":
        return ""
    if kind == "number":
        values = (definition.get("variants") or {}).values()
        return 0 if all(isinstance(v, int) and not isinstance(v, bool) for v in values) else 0.0
    return {}


def find_external_provider(container: Container) -> AbstractProvider | None:
    """The application's own OpenFeature provider bean: any bean whose class extends ``AbstractProvider``."""
    for cls in container.registered_types():
        if isinstance(cls, type) and issubclass(cls, AbstractProvider) and not issubclass(cls, FireflyFlagProvider):
            provider = container.resolve(cls)
            if isinstance(provider, AbstractProvider):
                return provider
    return None


def _options(preview: bool) -> FlagEvaluationOptions | None:
    return FlagEvaluationOptions(hook_hints={PREVIEW_HINT: True}) if preview else None


@contextlib.contextmanager
def _without_transaction_context() -> Iterator[None]:
    """Run the block under an empty transaction context, then put the previous one back (also on an exception).

    The context-variable propagator keeps the value per task (per thread outside a loop), so only this evaluation
    sees the empty context.
    """
    previous = get_transaction_context()
    set_transaction_context(EvaluationContext())
    try:
        yield
    finally:
        set_transaction_context(previous)


class FeatureFlags:
    """Typed flag evaluation with the ambient context (see the module documentation)."""

    def __init__(
        self, client: OpenFeatureClient, resolver: EvaluationContextResolver, *, registry: FlagRegistry | None = None
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._registry = registry

    @property
    def client(self) -> OpenFeatureClient:
        return self._client

    @property
    def registry(self) -> FlagRegistry | None:
        return self._registry

    def evaluation_context(
        self, context: Mapping[str, Any] | None = None, *, targeting_key: str | None = None, ambient: bool = True
    ) -> EvaluationContext:
        """The ambient context (or only the process attributes when not *ambient*), *context* on top."""
        if ambient:
            base = self._resolver.resolve()
            attributes: dict[str, Any] = dict(base.attributes)
            key = base.targeting_key
        else:
            attributes = self._resolver.process_attributes()
            key = None
        if context:
            explicit = dict(context)
            explicit_key = explicit.pop("targetingKey", None)
            attributes.update(explicit)
            if explicit_key is not None:
                key = str(explicit_key)
        if targeting_key is not None:
            key = targeting_key
        return EvaluationContext(targeting_key=key, attributes=attributes)

    # -- synchronous -----------------------------------------------------------------------------------------

    def _evaluate(
        self, key: str, default: FlagValueType, context: EvaluationContext, options: FlagEvaluationOptions | None
    ) -> FlagEvaluationDetails[Any]:
        if isinstance(default, bool):
            return self._client.get_boolean_details(key, default, context, options)
        if isinstance(default, str):
            return self._client.get_string_details(key, default, context, options)
        if isinstance(default, int):
            return self._client.get_integer_details(key, default, context, options)
        if isinstance(default, float):
            return self._client.get_float_details(key, default, context, options)
        return self._client.get_object_details(key, default, context, options)

    def details(
        self,
        key: str,
        default: FlagValueType,
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
        ambient: bool = True,
        preview: bool = False,
    ) -> FlagEvaluationDetails[Any]:
        """Evaluate *key* with the type of *default* (bool, str, int, float, or object).

        Not *ambient*: only *context*/*targeting_key* and the process attributes, under an empty transaction
        context. *preview* adds the :data:`PREVIEW_HINT` hook hint.
        """
        ctx = self.evaluation_context(context, targeting_key=targeting_key, ambient=ambient)
        options = _options(preview)
        if ambient:
            return self._evaluate(key, default, ctx, options)
        with _without_transaction_context():
            return self._evaluate(key, default, ctx, options)

    def is_enabled(
        self,
        key: str,
        default: bool = False,
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> bool:
        return bool(self.details(key, default, context=context, targeting_key=targeting_key).value)

    def get_string(
        self, key: str, default: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> str:
        return str(self.details(key, default, context=context, targeting_key=targeting_key).value)

    def get_int(
        self, key: str, default: int, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> int:
        return int(self.details(key, default, context=context, targeting_key=targeting_key).value)

    def get_float(
        self, key: str, default: float, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> float:
        return float(self.details(key, default, context=context, targeting_key=targeting_key).value)

    def get_object(
        self,
        key: str,
        default: Mapping[str, FlagValueType] | Sequence[FlagValueType],
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> Any:
        return self.details(key, default, context=context, targeting_key=targeting_key).value

    def _variant_default(self, key: str) -> FlagValueType:
        flag = self._registry.effective_flag(key) if self._registry is not None else None
        return typed_default(flag.definition) if flag is not None else ""

    def variant_details(
        self, key: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> FlagEvaluationDetails[Any]:
        """Evaluate *key* with the flag's own type (Firefly's provider), or as a string (an external one)."""
        return self.details(key, self._variant_default(key), context=context, targeting_key=targeting_key)

    def variant(
        self, key: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> str | None:
        return self.variant_details(key, context=context, targeting_key=targeting_key).variant

    # -- asynchronous ----------------------------------------------------------------------------------------

    async def _evaluate_async(
        self, key: str, default: FlagValueType, context: EvaluationContext, options: FlagEvaluationOptions | None
    ) -> FlagEvaluationDetails[Any]:
        if isinstance(default, bool):
            return await self._client.get_boolean_details_async(key, default, context, options)
        if isinstance(default, str):
            return await self._client.get_string_details_async(key, default, context, options)
        if isinstance(default, int):
            return await self._client.get_integer_details_async(key, default, context, options)
        if isinstance(default, float):
            return await self._client.get_float_details_async(key, default, context, options)
        return await self._client.get_object_details_async(key, default, context, options)

    async def details_async(
        self,
        key: str,
        default: FlagValueType,
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
        ambient: bool = True,
        preview: bool = False,
    ) -> FlagEvaluationDetails[Any]:
        """:meth:`details`, through the providers' asynchronous resolution."""
        ctx = self.evaluation_context(context, targeting_key=targeting_key, ambient=ambient)
        options = _options(preview)
        if ambient:
            return await self._evaluate_async(key, default, ctx, options)
        with _without_transaction_context():
            return await self._evaluate_async(key, default, ctx, options)

    async def is_enabled_async(
        self,
        key: str,
        default: bool = False,
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> bool:
        return bool((await self.details_async(key, default, context=context, targeting_key=targeting_key)).value)

    async def get_string_async(
        self, key: str, default: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> str:
        return str((await self.details_async(key, default, context=context, targeting_key=targeting_key)).value)

    async def get_int_async(
        self, key: str, default: int, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> int:
        return int((await self.details_async(key, default, context=context, targeting_key=targeting_key)).value)

    async def get_float_async(
        self, key: str, default: float, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> float:
        return float((await self.details_async(key, default, context=context, targeting_key=targeting_key)).value)

    async def get_object_async(
        self,
        key: str,
        default: Mapping[str, FlagValueType] | Sequence[FlagValueType],
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> Any:
        return (await self.details_async(key, default, context=context, targeting_key=targeting_key)).value

    async def variant_details_async(
        self, key: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> FlagEvaluationDetails[Any]:
        return await self.details_async(key, self._variant_default(key), context=context, targeting_key=targeting_key)

    async def variant_async(
        self, key: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> str | None:
        return (await self.variant_details_async(key, context=context, targeting_key=targeting_key)).variant


def _current_propagator() -> object:
    # The SDK exposes no getter; the module global is the installed propagator.
    return getattr(_transaction_context, "_evaluation_transaction_context_propagator", None)


class OpenFeatureBinding:
    """Installs the provider, the transaction-context propagator and the gating slot for the context's lifetime.

    A context may stop and start again: every start installs anew and every stop removes only what that start
    installed and is still installed, putting back the propagator it found. A second ``start`` (or ``stop``) in a
    row does nothing.
    """

    def __init__(
        self,
        provider: FeatureProvider | None,
        facade: FeatureFlags,
        *,
        domain: str | None = None,
        disabled_status: int = 404,
    ) -> None:
        self._provider = provider
        self._facade = facade
        self._domain = domain or None
        self._disabled_status = disabled_status
        self._propagator: ContextVarsTransactionContextPropagator | None = None
        self._previous_propagator: TransactionContextPropagator | None = None
        self._running = False

    @property
    def provider(self) -> FeatureProvider | None:
        return self._provider

    async def start(self) -> None:
        if self._running:
            return
        if self._provider is None:
            _logger.warning("feature_flags_no_provider", extra={"domain": self._domain})
        else:
            api.set_provider_and_wait(self._provider, self._domain)
        previous = _current_propagator()  # TransactionContextPropagator is not runtime-checkable: duck-type it
        has_api = hasattr(previous, "get_transaction_context")
        self._previous_propagator = cast("TransactionContextPropagator", previous) if has_api else None
        self._propagator = ContextVarsTransactionContextPropagator()
        set_transaction_context_propagator(self._propagator)
        install_feature_flags(self._facade, disabled_status=self._disabled_status)
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        uninstall_feature_flags(self._facade)
        for hook in self._facade.client.hooks:
            drain = getattr(hook, "drain", None)
            if callable(drain):
                try:
                    await drain()
                except Exception:  # noqa: BLE001 — stopping goes on: the provider and the propagator are restored
                    _logger.warning(
                        "feature_flags_hook_drain_failed", extra={"hook": type(hook).__name__}, exc_info=True
                    )
        if self._provider is not None:
            installed = OpenFeatureClient(domain=self._domain, version=None).provider
            if installed is self._provider:
                api.set_provider_and_wait(NoOpProvider(), self._domain)
        if self._propagator is not None and _current_propagator() is self._propagator:
            if self._previous_propagator is not None:
                set_transaction_context_propagator(self._previous_propagator)
            else:
                _transaction_context.clear_transaction_context_propagator()
        self._propagator = None
        self._previous_propagator = None
