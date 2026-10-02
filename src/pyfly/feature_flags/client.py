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
import inspect
import logging
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

import openfeature.transaction_context as _transaction_context
from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, FlagEvaluationOptions, FlagType, FlagValueType
from openfeature.provider import AbstractProvider, FeatureProvider
from openfeature.provider._registry import provider_registry
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
from pyfly.feature_flags.registry import FEATURE_FLAGS_PHASE, FeatureFlagsError
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
    """The application's own OpenFeature provider bean: the bean whose class extends ``AbstractProvider``.

    Firefly's provider is never one. One bean registered under several types counts once; two distinct provider
    beans raise :class:`~pyfly.feature_flags.registry.FeatureFlagsError` (which one serves the flags is ambiguous).
    """
    found: dict[int, AbstractProvider] = {}
    for cls in container.registered_types():
        if isinstance(cls, type) and issubclass(cls, AbstractProvider) and not issubclass(cls, FireflyFlagProvider):
            provider = container.resolve(cls)
            if isinstance(provider, AbstractProvider) and not isinstance(provider, FireflyFlagProvider):
                found.setdefault(id(provider), provider)
    if len(found) > 1:
        names = ", ".join(type(provider).__qualname__ for provider in found.values())
        raise FeatureFlagsError(f"expected a single OpenFeature provider bean but found {len(found)}: {names}")
    return next(iter(found.values()), None)


def _options(preview: bool) -> FlagEvaluationOptions | None:
    return FlagEvaluationOptions(hook_hints={PREVIEW_HINT: True}) if preview else None


def _type_of(default: FlagValueType) -> FlagType:
    """The OpenFeature type of *default* (checked for ``bool`` before ``int``: a bool is an int)."""
    if isinstance(default, bool):
        return FlagType.BOOLEAN
    if isinstance(default, str):
        return FlagType.STRING
    if isinstance(default, int):
        return FlagType.INTEGER
    if isinstance(default, float):
        return FlagType.FLOAT
    return FlagType.OBJECT


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
    """Typed flag evaluation with the ambient context (see the module documentation).

    Each typed getter evaluates with its own type, whatever the runtime type of the default: ``get_float(key, 1)``
    is a float evaluation and ``get_int(key, True)`` an integer one. The default is converted to that type first
    (``bool()``, ``str()``, ``int()``, ``float()``: ``int(True)`` is ``1``), so a getter always answers its own type.
    :meth:`details` takes the type of its default instead.
    """

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
        self,
        kind: FlagType,
        key: str,
        default: FlagValueType,
        *,
        context: Mapping[str, Any] | None,
        targeting_key: str | None,
        ambient: bool = True,
        preview: bool = False,
    ) -> FlagEvaluationDetails[Any]:
        ctx = self.evaluation_context(context, targeting_key=targeting_key, ambient=ambient)
        options = _options(preview)
        if ambient:
            return self._client.evaluate_flag_details(kind, key, default, ctx, options)
        with _without_transaction_context():
            return self._client.evaluate_flag_details(kind, key, default, ctx, options)

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
        return self._evaluate(
            _type_of(default),
            key,
            default,
            context=context,
            targeting_key=targeting_key,
            ambient=ambient,
            preview=preview,
        )

    def is_enabled(
        self,
        key: str,
        default: bool = False,
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> bool:
        details = self._evaluate(FlagType.BOOLEAN, key, bool(default), context=context, targeting_key=targeting_key)
        return bool(details.value)

    def get_string(
        self, key: str, default: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> str:
        details = self._evaluate(FlagType.STRING, key, str(default), context=context, targeting_key=targeting_key)
        return str(details.value)

    def get_int(
        self, key: str, default: int, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> int:
        details = self._evaluate(FlagType.INTEGER, key, int(default), context=context, targeting_key=targeting_key)
        return int(details.value)

    def get_float(
        self, key: str, default: float, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> float:
        details = self._evaluate(FlagType.FLOAT, key, float(default), context=context, targeting_key=targeting_key)
        return float(details.value)

    def get_object(
        self,
        key: str,
        default: Mapping[str, FlagValueType] | Sequence[FlagValueType],
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> Any:
        return self._evaluate(FlagType.OBJECT, key, default, context=context, targeting_key=targeting_key).value

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
        self,
        kind: FlagType,
        key: str,
        default: FlagValueType,
        *,
        context: Mapping[str, Any] | None,
        targeting_key: str | None,
        ambient: bool = True,
        preview: bool = False,
    ) -> FlagEvaluationDetails[Any]:
        ctx = self.evaluation_context(context, targeting_key=targeting_key, ambient=ambient)
        options = _options(preview)
        if ambient:
            return await self._client.evaluate_flag_details_async(kind, key, default, ctx, options)
        with _without_transaction_context():
            return await self._client.evaluate_flag_details_async(kind, key, default, ctx, options)

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
        return await self._evaluate_async(
            _type_of(default),
            key,
            default,
            context=context,
            targeting_key=targeting_key,
            ambient=ambient,
            preview=preview,
        )

    async def is_enabled_async(
        self,
        key: str,
        default: bool = False,
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> bool:
        details = await self._evaluate_async(
            FlagType.BOOLEAN, key, bool(default), context=context, targeting_key=targeting_key
        )
        return bool(details.value)

    async def get_string_async(
        self, key: str, default: str, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> str:
        details = await self._evaluate_async(
            FlagType.STRING, key, str(default), context=context, targeting_key=targeting_key
        )
        return str(details.value)

    async def get_int_async(
        self, key: str, default: int, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> int:
        details = await self._evaluate_async(
            FlagType.INTEGER, key, int(default), context=context, targeting_key=targeting_key
        )
        return int(details.value)

    async def get_float_async(
        self, key: str, default: float, *, context: Mapping[str, Any] | None = None, targeting_key: str | None = None
    ) -> float:
        details = await self._evaluate_async(
            FlagType.FLOAT, key, float(default), context=context, targeting_key=targeting_key
        )
        return float(details.value)

    async def get_object_async(
        self,
        key: str,
        default: Mapping[str, FlagValueType] | Sequence[FlagValueType],
        *,
        context: Mapping[str, Any] | None = None,
        targeting_key: str | None = None,
    ) -> Any:
        details = await self._evaluate_async(
            FlagType.OBJECT, key, default, context=context, targeting_key=targeting_key
        )
        return details.value

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


# The SDK can replace the provider of a domain (``set_provider``) but neither tells an unbound domain from one bound to
# the default provider nor unbinds one. The two helpers below therefore read and edit its registry's ``_providers``
# map, under its lock; should that change, each falls back to the closest public behavior it documents.


def _bound_provider(domain: str | None) -> FeatureProvider | None:
    """The provider bound to *domain* itself: the default provider for ``None``; for a named domain, ``None`` when
    nothing is bound to it (it falls back to the default provider). Without the registry's map, a named domain that
    answers the default provider counts as unbound."""
    if domain is None:
        return provider_registry.get_default_provider()
    providers = getattr(provider_registry, "_providers", None)
    if isinstance(providers, dict):
        return cast("FeatureProvider | None", providers.get(domain))
    current = provider_registry.get_provider(domain)
    return None if current is provider_registry.get_default_provider() else current


def _unbind(domain: str, provider: FeatureProvider) -> None:
    """Remove *domain*'s binding to *provider*, so the domain falls back to the default provider again, and let the
    SDK shut *provider* down once nothing uses it (what ``set_provider`` does to the provider it replaces). A domain
    bound to another provider by now is left alone. Without the registry's map and lock, the closest public behavior:
    the domain is bound to a no-op provider (it then no longer falls back to the default one)."""
    providers = getattr(provider_registry, "_providers", None)
    lock = getattr(provider_registry, "_lock", None)
    if not isinstance(providers, dict) or lock is None:
        api.set_provider_and_wait(NoOpProvider(), domain)
        return
    with lock:
        if providers.get(domain) is not provider:
            return
        del providers[domain]
    shutdown_if_unused = getattr(provider_registry, "_shutdown_if_unused", None)
    if callable(shutdown_if_unused):
        shutdown_if_unused(provider)


# A provider whose binding stopped while another binding had replaced it in its domain (so it could not put back what
# it had found), by identity, with what it had found there. Bindings may stop in any order: the binding that later
# puts that provider back puts back what it stands for instead, never a stopped context's provider.
_superseded: dict[int, tuple[FeatureProvider, FeatureProvider | None]] = {}
_superseded_lock = threading.Lock()


def _supersede(provider: FeatureProvider, found: FeatureProvider | None) -> None:
    with _superseded_lock:
        _superseded[id(provider)] = (provider, found)


def _to_put_back(found: FeatureProvider | None, own: FeatureProvider) -> FeatureProvider | None:
    """What to put back for *found*: *found* itself, or, while it is a superseded provider, what its binding had
    found (each entry is used once). A binding never puts its own provider back: that counts as nothing found."""
    seen: set[int] = set()
    with _superseded_lock:
        while found is not None and id(found) not in seen:
            seen.add(id(found))
            entry = _superseded.get(id(found))
            if entry is None or entry[0] is not found:
                break
            del _superseded[id(found)]
            found = entry[1]
    return None if found is own else found


class OpenFeatureBinding:
    """Installs the provider, the transaction-context propagator and the gating slot for the context's lifetime.

    ``start`` records each piece as it installs it (the provider before the SDK call, which binds the provider
    before initializing it and re-raises a failed initialization), and ``stop`` undoes exactly what was recorded,
    each piece only if it is still the installed one, putting back the propagator found at start. ``stop`` does so
    after a ``start`` that failed half-way too, and when it is cancelled while draining the hooks (the cancellation
    is re-raised once everything is restored). A context may therefore stop and start again; a second ``start`` (or
    ``stop``) in a row does nothing.

    ``start`` records what the domain held (for a named domain, possibly nothing: it fell back to the default
    provider), and ``stop`` puts exactly that back, if the binding's provider is still the one installed: the
    provider it replaced (the SDK shut that provider down when it was replaced and initializes it again), or, for a
    named domain that held none, no binding at all, so the domain falls back to the default provider again. When
    bindings that share a domain stop in another order than they started, the last one to stop puts back what the
    first one found, never a stopped context's provider.

    Installing over a provider bound to the domain that is neither the no-op one nor the binding's own logs
    ``feature_flags_provider_replaced`` (WARNING, naming the domain: ``None`` is the default one); a named domain
    that only falls back to the default provider replaces nothing. A framework client that does not reach the
    installed provider afterwards logs ``feature_flags_client_domain_shadowed``.

    It starts in :data:`~pyfly.feature_flags.registry.FEATURE_FLAGS_PHASE`, after the registry (created first, so
    started first in the phase) and before the application's lifecycle beans, which therefore see the flags and the
    gating slot in their ``start()`` and ``stop()``.
    """

    phase = FEATURE_FLAGS_PHASE

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
        self._running = False
        # what start installed, so stop undoes exactly that
        self._provider_installed = False
        self._found_provider: FeatureProvider | None = None  # what the domain held before (None: nothing bound)
        self._propagator: ContextVarsTransactionContextPropagator | None = None
        self._found_propagator: TransactionContextPropagator | None = None
        self._slot_installed = False

    @property
    def provider(self) -> FeatureProvider | None:
        return self._provider

    async def start(self) -> None:
        if self._running:
            return
        if self._provider is None:
            _logger.warning("feature_flags_no_provider", extra={"domain": self._domain})
        else:
            bound = _bound_provider(self._domain)
            self._warn_if_replacing(bound)
            self._found_provider = None if bound is self._provider else bound  # its own provider is never put back
            self._provider_installed = True  # before the call: a failed initialization leaves the provider bound
            api.set_provider_and_wait(self._provider, self._domain)
            self._warn_if_shadowed()
        found = _current_propagator()  # TransactionContextPropagator is not runtime-checkable: duck-type it
        has_api = hasattr(found, "get_transaction_context")
        self._found_propagator = cast("TransactionContextPropagator", found) if has_api else None
        self._propagator = ContextVarsTransactionContextPropagator()
        set_transaction_context_propagator(self._propagator)
        self._slot_installed = True
        install_feature_flags(self._facade, disabled_status=self._disabled_status)
        self._running = True

    def _warn_if_replacing(self, found: FeatureProvider | None) -> None:
        """Warn when *found*, the provider bound to the domain, is neither the no-op one nor this binding's own: the
        OpenFeature API is process-global, so another context sharing the domain (two applications on the default
        domain) or the application's own code loses its provider to this one until the binding stops. A named
        domain bound to nothing (it falls back to the application's default provider) replaces nothing."""
        if found is not None and not isinstance(found, NoOpProvider) and found is not self._provider:
            _logger.warning(
                "feature_flags_provider_replaced",
                extra={"domain": self._domain, "provider": type(found).__qualname__},
            )

    def _warn_if_shadowed(self) -> None:
        """Warn when the framework's client does not reach the provider just installed: with the default domain the
        client is in the ``firefly`` domain, which falls back to the default provider only while no provider is
        bound to ``firefly`` itself (an application domain named ``firefly``)."""
        reached = self._facade.client.provider
        if reached is not self._provider:
            _logger.warning(
                "feature_flags_client_domain_shadowed",
                extra={"domain": self._facade.client.domain, "provider": type(reached).__qualname__},
            )

    async def stop(self) -> None:
        was_running, self._running = self._running, False
        self._uninstall_slot()
        try:
            if was_running:
                await self._drain_hooks()
        finally:
            self._restore_propagator()
            self._uninstall_provider()

    async def _drain_hooks(self) -> None:
        """Await every hook's coroutine ``drain()`` (the exposure hook's); a failing one is logged."""
        for hook in self._facade.client.hooks:
            drain = getattr(hook, "drain", None)
            if not inspect.iscoroutinefunction(drain):
                continue
            try:
                await drain()
            except Exception:  # noqa: BLE001 — stopping goes on: the provider and the propagator are restored
                _logger.warning("feature_flags_hook_drain_failed", extra={"hook": type(hook).__name__}, exc_info=True)

    def _uninstall_slot(self) -> None:
        if self._slot_installed:
            self._slot_installed = False
            uninstall_feature_flags(self._facade)

    def _restore_propagator(self) -> None:
        if self._propagator is None:
            return
        if _current_propagator() is self._propagator:
            if self._found_propagator is not None:
                set_transaction_context_propagator(self._found_propagator)
            else:
                _transaction_context.clear_transaction_context_propagator()
        self._propagator = None
        self._found_propagator = None

    def _uninstall_provider(self) -> None:
        if not self._provider_installed:
            return
        self._provider_installed = False
        provider, found, self._found_provider = self._provider, self._found_provider, None
        if provider is None:
            return
        if _bound_provider(self._domain) is not provider:
            _supersede(provider, found)  # another binding replaced it: it puts back what this one found
            return
        self._put_back(provider, _to_put_back(found, provider))

    def _put_back(self, provider: FeatureProvider, found: FeatureProvider | None) -> None:
        """Put *found* back in the domain: the provider ``start`` replaced, or no binding (a named domain) or the
        no-op provider (the default domain) when there was none. A failure is logged; stopping goes on."""
        try:
            if found is None and self._domain is not None:
                _unbind(self._domain, provider)
            else:
                api.set_provider_and_wait(found if found is not None else NoOpProvider(), self._domain)
        except Exception:  # noqa: BLE001 — stopping goes on: the propagator and the slot are restored regardless
            _logger.warning(
                "feature_flags_provider_restore_failed",
                extra={"domain": self._domain, "provider": type(found).__qualname__ if found is not None else None},
                exc_info=True,
            )
