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
"""``FlagRegistry``: composes the flag sources, keeps each one's last good document, feeds the provider (spec 4.5).

A lifecycle bean: :meth:`FlagRegistry.start` loads every source once (a ``fail_fast`` source that cannot load fails
startup with :class:`~pyfly.feature_flags.sources.FlagSourceError`; the others start empty and report ``DOWN``),
composes, pushes the composed flagd document into :class:`~pyfly.feature_flags.provider.FireflyFlagProvider`, logs
one warning per expired flag, then polls every source that has a ``refresh_interval`` on an asyncio task of its own.

A composition that changes the effective set publishes :class:`~pyfly.feature_flags.events.FeatureFlagsChanged`. The
registry computes its ``changed_keys`` itself, by comparing the two compositions: FlagdCore's own diff sees neither
the fields it does not read nor the document ``metadata`` a flag inherits. The same keys go to the provider, whose
``PROVIDER_CONFIGURATION_CHANGED`` carries them, so OpenFeature handlers and listeners see one change signal.

A validated document gives FlagdCore nothing to refuse, but the provider is still guarded: when it refuses a composed
document, the composition in force stays (and ``feature_flag_provider_update_failed`` is logged at ERROR), and then

- at startup, :meth:`FlagRegistry.start` raises :class:`FeatureFlagsError` naming the provider's error (spec 4.5:
  an invalid boot definition fails startup; which layer is at fault is unknown, so no source is named);
- after a refresh, the refreshed source keeps its last good document (and its revision and ``last_refresh``),
  reports ``STALE`` with the provider's reason and logs ``feature_flag_source_failed`` at WARNING; the reason stands,
  even across a later failed load, until the source delivers a new document;
- after a test-override change, the previous overrides are restored and :class:`FeatureFlagsError` is raised to the
  test.

A source's failures are logged when they change, not on every poll: ``feature_flag_source_failed`` at WARNING when the
source starts failing or its error text changes, at DEBUG while the same error repeats, and
``feature_flag_source_recovered`` at INFO when a load succeeds again.

Test overrides may be set from any thread (a test driving the application through Starlette's ``TestClient`` sets them
from a thread without a loop): one lock serializes every change of the layers and its recomposition, and the
``FeatureFlagsChanged`` of an override change is published on the loop the registry started on, handed over from
another thread, or, when that loop is gone and the calling thread runs none, through a short-lived loop. It is never
dropped.

The registry starts in :data:`FEATURE_FLAGS_PHASE`. The context starts its lifecycle beans before it wires the
``@app_event_listener`` methods, so a registry built with ``hold_events=True`` (the auto-configuration's) keeps the
events it publishes (the boot composition's, and any refresh before the context is refreshed) and releases them, in
order and once, when ``ContextRefreshedEvent`` reaches :meth:`FlagRegistry.on_context_refreshed`, itself an
``@app_event_listener``. A stop before that drops them. A standalone registry publishes at once.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

from pyfly.context.events import ContextRefreshedEvent, app_event_listener
from pyfly.feature_flags._references import referenced_evaluator
from pyfly.feature_flags.composition import ComposedFlag, Composition, Layer, compose
from pyfly.feature_flags.definitions import FlagDocument, expired_keys, parse_document, utc_today
from pyfly.feature_flags.events import FeatureFlagsChanged
from pyfly.feature_flags.sources import FlagSource, FlagSourceError, SourceSnapshot

if TYPE_CHECKING:
    from pyfly.context.events import ApplicationEventPublisher
    from pyfly.feature_flags.provider import FireflyFlagProvider

__all__ = ["FEATURE_FLAGS_PHASE", "STARTUP", "TEST_OVERRIDES", "FeatureFlagsError", "FlagRegistry", "SourceStatus"]

_logger = logging.getLogger(__name__)

TEST_OVERRIDES = "test-overrides"
"""The name of the test-override layer (the highest), installed by ``pyfly.testing.feature_flags``."""

STARTUP = "startup"
"""The ``origin`` of the boot composition's ``FeatureFlagsChanged``."""

FEATURE_FLAGS_PHASE = -(1 << 10)
"""The lifecycle phase of the registry and of the OpenFeature binding (``pyfly.kernel.lifecycle``).

Above the datasource, the migrations and the schema (``-(1 << 20)`` and below), which the database store needs, and
below the default phase 0 of the application's lifecycle beans: those start once the flags are loaded and the
provider and the gating slot are installed, and stop while they still are. (A user ``@configuration``'s lifecycle
beans are created before the auto-configuration's, so creation order alone would start them first.)"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- change detection ----------------------------------------------------------------------------------------------


def _same(left: Any, right: Any) -> bool:
    """Equality as JSON sees it: ``True`` is not ``1`` and ``1`` is not ``1.0``, although Python's ``==`` says so."""
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return type(left) is type(right) and bool(left == right)


def _refs(value: Any, names: set[str]) -> None:
    """Add to *names* every evaluator *value* references, at any depth: the provider's rule, a ``{"$ref": name}``
    object with exactly one key (an object that also holds other keys is not a reference)."""
    if (name := referenced_evaluator(value)) is not None:
        names.add(name)
    elif isinstance(value, Mapping):
        for item in value.values():
            _refs(item, names)
    elif isinstance(value, list | tuple):
        for item in value:
            _refs(item, names)


def _evaluators_used(definition: Mapping[str, Any], evaluators: Mapping[str, Any]) -> set[str]:
    """The evaluators *definition* depends on: the ones its ``targeting`` ``$ref``s and, transitively, the ones those
    ``$ref`` (the provider expands references in ``targeting`` only, spec 4.1)."""
    pending: set[str] = set()
    _refs(definition.get("targeting"), pending)
    used: set[str] = set()
    while pending:
        name = pending.pop()
        used.add(name)
        if name in evaluators:
            nested: set[str] = set()
            _refs(evaluators[name], nested)
            pending |= nested - used
    return used


def _effective_metadata(definition: Mapping[str, Any], document_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """The metadata an evaluation reports: the document's, overridden key by key by the flag's own (as flagd does)."""
    own = definition.get("metadata")
    return {**document_metadata, **(own if isinstance(own, Mapping) else {})}


def _changed_keys(before: Composition, after: Composition) -> list[str]:
    """The keys whose effective flag differs between two compositions, sorted.

    A key changes when it appears or disappears, when any field of its composed definition differs (``metadata`` and
    the fields flagd does not read included), when an evaluator it depends on differs, or when the document
    ``metadata`` it inherits differs. Where the definition comes from (``origin``, ``overrides``) is not part of it.
    """
    evaluators = {
        name
        for name in before.evaluators.keys() | after.evaluators.keys()
        if not _same(before.evaluators.get(name), after.evaluators.get(name))
    }
    changed = before.flags.keys() ^ after.flags.keys()
    for key in before.flags.keys() & after.flags.keys():
        old, new = before.flags[key].definition, after.flags[key].definition
        if (
            not _same(old, new)
            or not _same(_effective_metadata(old, before.metadata), _effective_metadata(new, after.metadata))
            or not evaluators.isdisjoint(_evaluators_used(new, after.evaluators))
        ):
            changed.add(key)
    return sorted(changed)


class FeatureFlagsError(RuntimeError):
    """The provider refused a composition the registry had to put in force: the boot composition (startup fails) or
    a test-override change (rolled back). The message names the provider's error, which is chained as the cause."""


class _ProviderRefused(Exception):
    """The provider refused the composed document (already logged); the composition in force stays."""


# -- the registry --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceStatus:
    """A source as the actuator and the health indicator report it."""

    name: str
    enabled: bool
    status: str
    flags: int
    last_refresh: datetime | None
    error: str | None
    revision: str | None


class _SourceState:
    def __init__(self, source: FlagSource) -> None:
        self.source = source
        self.document: FlagDocument | None = None
        self.revision: str | None = None
        self.last_refresh: datetime | None = None
        self.error: str | None = None
        self.refusal: str | None = None  # why the provider refused the source's latest document, until a new one
        self.started = 0
        self.applied = 0

    @property
    def status(self) -> str:
        if self.document is None:
            return "DOWN"
        return "UP" if self.error is None else "STALE"


class FlagRegistry:
    """The effective flag set of the application (see the module documentation).

    *hold_events*: keep every event published before :meth:`on_context_refreshed`, then release them in order (the
    auto-configuration passes ``True``; see the module documentation).
    """

    phase = FEATURE_FLAGS_PHASE

    def __init__(
        self,
        sources: Sequence[FlagSource],
        provider: FireflyFlagProvider,
        *,
        publisher: ApplicationEventPublisher | None = None,
        today: Callable[[], date] = utc_today,
        clock: Callable[[], datetime] = _utc_now,
        hold_events: bool = False,
    ) -> None:
        names = [source.name for source in sources]
        if len(set(names)) != len(names):
            raise ValueError(f"feature flag source names must be unique: {names}")
        if TEST_OVERRIDES in names:
            raise ValueError(f"the source name {TEST_OVERRIDES!r} is reserved for test overrides")
        self._states = [_SourceState(source) for source in sources]
        self._by_name = {state.source.name: state for state in self._states}
        self._provider = provider
        self._publisher = publisher
        self._today = today
        self._clock = clock
        self._overrides: FlagDocument | None = None
        self._override_flags: dict[str, Any] = {}
        self._composition = Composition()
        self._layers_accepted = True  # the layers are the ones the composition in force was built from
        self._tasks: list[asyncio.Task[None]] = []
        # every change of the layers and its recomposition, from the loop or from a test's thread
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None  # the loop the registry started on
        # override publishes until they settle: a task of a running loop, or a future of the registry's loop
        self._pending: set[asyncio.Task[None] | concurrent.futures.Future[None]] = set()
        self._pending_lock = threading.Lock()  # the done callbacks and the override changes run on several threads
        self._started = False
        self._hold_events = hold_events
        self._holding = hold_events  # until the context is refreshed
        self._held: list[object] = []

    # -- lifecycle -------------------------------------------------------------------------------------------

    @property
    def provider(self) -> FireflyFlagProvider:
        return self._provider

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        for state in self._states:
            ticket = self._ticket(state)
            try:
                snapshot = await state.source.load()
            except Exception as error:
                if state.source.fail_fast:
                    raise FlagSourceError(state.source.name, error) from error
                self._failed(state, ticket, error)
                continue
            with self._lock:
                before = state.error
                self._loaded(state, ticket, snapshot)
                self._report(state, before)
        with self._lock:
            try:
                changed = self._recompose()
            except _ProviderRefused as refused:  # which layer the provider objects to is unknown: no source is named
                raise FeatureFlagsError(
                    f"the feature flag provider refused the startup composition: {refused}"
                ) from refused.__cause__
        for key in self.expired_keys():
            flag = self._composition.flags[key]
            _logger.warning(
                "feature_flag_expired",
                extra={"flag": key, "expires": flag.definition["metadata"]["expires"], "origin": flag.origin},
            )
        if changed:
            await self._publish(FeatureFlagsChanged(tuple(changed), STARTUP))
        self._started = True
        for state in self._states:
            interval = state.source.refresh_interval
            if interval is not None and interval > 0:
                name = state.source.name
                self._tasks.append(asyncio.create_task(self._poll(name, interval), name=f"pyfly-feature-flags-{name}"))

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._drain_pending()
        for state in self._states:
            try:
                await state.source.close()
            except Exception:  # noqa: BLE001 — stopping goes on
                _logger.warning("feature_flag_source_close_failed", extra={"source": state.source.name}, exc_info=True)
        if self._held:  # stopped before the context was refreshed: nobody is listening any more
            _logger.debug("feature_flag_events_dropped", extra={"events": len(self._held)})
        self._held = []
        self._holding = self._hold_events  # started again, the registry holds until the next refresh
        self._started = False

    @app_event_listener
    async def on_context_refreshed(self, event: ContextRefreshedEvent) -> None:
        """Release the held events, in the order they were published, once; from then on events are published at
        once. Events published while the held ones are being delivered join the end of the queue, so the order holds.
        A stopped registry ignores the event: it holds again until the refresh that follows its next start.
        """
        if not self._holding or not self._started:
            return
        while self._held:
            await self._deliver(self._held.pop(0))
        self._holding = False

    async def _poll(self, name: str, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh(name)
            except Exception:  # noqa: BLE001 — a refresh never ends its polling loop
                _logger.warning("feature_flag_refresh_failed", extra={"source": name}, exc_info=True)

    # -- refreshing ------------------------------------------------------------------------------------------

    async def refresh(self, name: str) -> list[str]:
        """Reload source *name* now; returns the keys whose effective definition changed."""
        state = self._by_name[name]
        ticket = self._ticket(state)
        try:
            snapshot = await state.source.load()
        except Exception as error:  # noqa: BLE001 — the last good document stays; status says why
            self._failed(state, ticket, error)
            return []
        with self._lock:
            changed = self._apply(state, ticket, snapshot)
        if changed:
            await self._publish(FeatureFlagsChanged(tuple(changed), name))
        return changed

    def _apply(self, state: _SourceState, ticket: int, snapshot: SourceSnapshot | None) -> list[str]:
        """Record a successful refresh of *state* and compose the new document it brought; the changed keys.

        The caller holds the lock. The source's status change is logged once the outcome is known (a new document
        the provider refuses is a failure, not a recovery)."""
        before = state.error
        last_good = (state.document, state.revision, state.last_refresh)
        attributable = self._layers_accepted  # every other layer is in the composition in force
        try:
            if not self._loaded(state, ticket, snapshot):
                return []
            try:
                return self._recompose()
            except _ProviderRefused as refused:
                if attributable:  # this document alone was refused: the source keeps its last good one
                    state.document, state.revision, state.last_refresh = last_good
                    state.error = state.refusal = str(refused)
                    self._layers_accepted = True
                return []
        finally:
            if state.applied == ticket:  # a load that finished after a newer one changed nothing
                self._report(state, before)

    async def refresh_all(self) -> list[str]:
        changed: set[str] = set()
        for state in self._states:
            changed.update(await self.refresh(state.source.name))
        return sorted(changed)

    @staticmethod
    def _ticket(state: _SourceState) -> int:
        state.started += 1
        return state.started

    def _loaded(self, state: _SourceState, ticket: int, snapshot: SourceSnapshot | None) -> bool:
        """Record a successful load; ``True`` when it brought a new document to compose."""
        if ticket < state.applied:
            return False  # a newer load already applied
        state.applied = ticket
        if snapshot is None and state.refusal is not None:
            state.error = state.refusal  # still the document the provider refused, whatever failed in between
            return False
        state.last_refresh = self._clock()
        state.error = None
        if snapshot is None:
            return False
        state.document = snapshot.document
        state.revision = snapshot.revision
        state.refusal = None
        return True

    def _failed(self, state: _SourceState, ticket: int, error: BaseException) -> None:
        with self._lock:
            if ticket < state.applied:
                return
            state.applied = ticket
            before = state.error
            state.error = f"{type(error).__name__}: {error}"
            self._report(state, before)

    @staticmethod
    def _report(state: _SourceState, before: str | None) -> None:
        """Log the change of *state*'s error from *before*: WARNING when the source starts failing or fails
        differently, DEBUG when the same error repeats, INFO when it recovers; nothing while it stays healthy."""
        after, name = state.error, state.source.name
        if after is None:
            if before is not None:
                _logger.info("feature_flag_source_recovered", extra={"source": name})
            return
        level = logging.DEBUG if after == before else logging.WARNING
        _logger.log(level, "feature_flag_source_failed", extra={"source": name, "error": after})

    def _layers(self, *, include_test_overrides: bool) -> list[Layer]:
        layers = [Layer(state.source.name, state.document) for state in self._states if state.document is not None]
        if include_test_overrides and self._overrides is not None:
            layers.append(Layer(TEST_OVERRIDES, self._overrides))
        return layers

    def _recompose(self) -> list[str]:
        """Compose the layers and push the result into the provider; returns the changed keys. The caller holds the
        lock.

        Composing and pushing are synchronous, so no reader sees a half-applied set. Raises :class:`_ProviderRefused`
        when the provider refuses the document, which keeps evaluating the previous one.
        """
        composition = compose(self._layers(include_test_overrides=True))
        changed = _changed_keys(self._composition, composition)
        try:
            self._provider.update(composition.to_flagd(), changed_keys=changed)
        except Exception as error:  # noqa: BLE001 — keep evaluating the previous set
            reason = f"{type(error).__name__}: {error}"
            self._layers_accepted = False
            _logger.error("feature_flag_provider_update_failed", extra={"error": reason}, exc_info=True)
            raise _ProviderRefused(reason) from error
        self._composition = composition
        self._layers_accepted = True
        return changed

    async def _publish(self, event: object) -> None:
        if self._publisher is None:
            return
        if self._holding:
            self._held.append(event)
            return
        await self._deliver(event)

    async def _deliver(self, event: object) -> None:
        publisher = self._publisher
        if publisher is None:
            return
        try:
            await publisher.publish(event)
        except Exception:  # noqa: BLE001 — a listener never breaks the flags
            _logger.warning("feature_flag_event_listener_failed", extra={"event": type(event).__name__}, exc_info=True)

    def _publish_soon(self, event: object) -> None:
        """Publish from synchronous code, on any thread: on the loop the registry started on while it runs (a task
        when that is the calling thread's loop, handed over from any other thread), otherwise on the calling thread's
        running loop, otherwise through a short-lived loop, synchronously. Never dropped."""
        loop = self._loop
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and loop is not running and loop.is_running():
            publish = self._publish(event)
            try:
                self._track(asyncio.run_coroutine_threadsafe(publish, loop))
                return
            except RuntimeError:
                publish.close()  # the registry's loop closed under us: nobody is left to run it
        if running is not None:
            self._track(running.create_task(self._publish(event)))
            return
        asyncio.run(self._publish(event))

    def _track(self, handle: asyncio.Task[None] | concurrent.futures.Future[None]) -> None:
        with self._pending_lock:
            self._pending.add(handle)
        handle.add_done_callback(self._settled)

    def _settled(self, handle: asyncio.Future[Any] | concurrent.futures.Future[Any]) -> None:
        with self._pending_lock:
            self._pending.discard(cast("Any", handle))

    async def _drain_pending(self) -> None:
        """Await every override publish scheduled so far that this loop can see finish: the tasks of this loop and
        the publishes handed over to the registry's loop. A task of another loop belongs to that loop."""
        current = asyncio.get_running_loop()
        while True:
            with self._pending_lock:
                handles = [handle for handle in self._pending if not handle.done()]
            waits = [
                asyncio.wrap_future(handle) if isinstance(handle, concurrent.futures.Future) else handle
                for handle in handles
                if isinstance(handle, concurrent.futures.Future) or handle.get_loop() is current
            ]
            if not waits:
                return
            await asyncio.gather(*waits, return_exceptions=True)

    # -- reading ---------------------------------------------------------------------------------------------

    def has_source(self, name: str) -> bool:
        return name in self._by_name

    def sources(self) -> list[SourceStatus]:
        with self._lock:
            return [
                SourceStatus(
                    name=state.source.name,
                    enabled=True,
                    status=state.status,
                    flags=len(state.document.flags) if state.document is not None else 0,
                    last_refresh=state.last_refresh,
                    error=state.error,
                    revision=state.revision,
                )
                for state in self._states
            ]

    def composition(self) -> Composition:
        """The composition in force, the registry's own object: **read-only**.

        The registry never mutates a composition (every change composes a new one), so it is safe to read from any
        thread, but neither may a caller: changing it, or a definition inside it, would desynchronize it from the
        document the provider evaluates. Copy it (``copy.deepcopy``) before building a changed definition.
        """
        return self._composition

    def effective_flag(self, key: str) -> ComposedFlag | None:
        """The composed flag *key* in force, or ``None``: the registry's own object, **read-only** (see
        :meth:`composition`); copy its ``definition`` before changing it."""
        return self._composition.flags.get(key)

    def layers(self, key: str) -> list[tuple[str, dict[str, Any]]]:
        """Each layer's definition of *key*, lowest precedence first, as ``(source, definition)``.

        Each definition is a shallow copy whose nested values (``variants``, ``targeting``, ``metadata``) are the
        layer's own: **read-only**, copy (``copy.deepcopy``) before changing.
        """
        with self._lock:
            return [
                (layer.source, dict(layer.document.flags[key]))
                for layer in self._layers(include_test_overrides=True)
                if key in layer.document.flags
            ]

    def document(self, *, include_test_overrides: bool = True) -> dict[str, Any]:
        """The effective set as a flagd document (a copy of its own); the sync server leaves test overrides out."""
        with self._lock:
            if include_test_overrides or self._overrides is None:
                return self._composition.to_flagd()
            return compose(self._layers(include_test_overrides=False)).to_flagd()

    def expired_keys(self) -> list[str]:
        flags = {key: flag.definition for key, flag in self._composition.flags.items()}
        return expired_keys(flags, self._today())

    # -- test overrides --------------------------------------------------------------------------------------

    @property
    def test_overrides(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._override_flags)

    def set_test_overrides(self, flags: Mapping[str, Any]) -> list[str]:
        """Install *flags* (shorthand allowed) as the highest layer; returns the changed keys.

        Raises :class:`~pyfly.feature_flags.definitions.FlagDefinitionError` for an invalid definition, and
        :class:`FeatureFlagsError` when the provider refuses the result; either way the previous overrides stay. Safe
        from any thread (see the module documentation).
        """
        return self._replace_overrides(parse_document({"flags": dict(flags)}, shorthand=True), dict(flags))

    def clear_test_overrides(self) -> list[str]:
        """Remove the test overrides; returns the changed keys (:class:`FeatureFlagsError` as in ``set``)."""
        return self._replace_overrides(None, {})

    def _replace_overrides(self, overrides: FlagDocument | None, flags: dict[str, Any]) -> list[str]:
        with self._lock:
            previous = self._overrides, self._override_flags, self._layers_accepted
            self._overrides, self._override_flags = overrides, flags
            try:
                changed = self._recompose()
            except _ProviderRefused as refused:  # the layers are the ones in force again
                self._overrides, self._override_flags, self._layers_accepted = previous
                raise FeatureFlagsError(
                    f"the feature flag provider refused the test overrides: {refused}"
                ) from refused.__cause__
        if changed:
            self._publish_soon(FeatureFlagsChanged(tuple(changed), TEST_OVERRIDES))
        return changed
