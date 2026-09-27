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
"""The listener container: how a broker delivery becomes a committed unit of work, then an acknowledgement.

The Kafka and RabbitMQ adapters of :mod:`pyfly.messaging` and the Kafka and RabbitMQ buses of
:mod:`pyfly.eda` consume through the two containers of this module, so every consumer in the framework
gives the same guarantees (at-least-once, Spring Kafka and Spring AMQP semantics):

- **Each delivery runs in a unit of work the container opens** (``REQUIRED`` on the configured
  datasource, the default one unless ``listener.datasource`` names another). The handler's
  ``@transactional`` joins it, and so do its repository calls. The delivery is acknowledged (the Kafka
  offset committed, the AMQP message acked) only once that unit has committed. An application without a
  data layer runs its handlers without a unit, and acknowledges once they return.
- **A failed delivery is attempted again** after a back-off whose default is not zero (1 s, doubling, at
  most 30 s, 5 attempts in all). Kafka seeks the partition back to the record and pauses it for the
  delay, so the record is fetched again and the partition keeps its order; RabbitMQ holds the message for
  the delay and republishes it to its queue with the attempt count in the ``x-pyfly-delivery-attempt``
  header, then acks the original. A message the adapter cannot read (:class:`PoisonMessageError`), and
  an exception type the policy lists as not retryable, skip the remaining attempts; a transient failure
  (:func:`is_transient_failure`: a lost connection, a timeout, a lock or serialization conflict) is always
  attempted again.
- **After the last attempt the delivery is dead-lettered** (``<topic>.DLT`` on Kafka, an exchange the
  adapter declares on RabbitMQ) and only then acknowledged. When the dead-letter publish fails, nothing
  is acknowledged: the delivery comes back and is dead-lettered again. Switching dead-lettering off
  restores log-and-skip after the last attempt, as an explicit choice.
- **Bounded concurrency.** Kafka runs one record at a time per consumer. RabbitMQ sets a prefetch per
  consumer channel (``basic.qos``, 20 by default), and all the consumers of one adapter share a
  concurrency limit sized from the datasource's connection pool (one handler at a time on SQLite), so a
  backlog never becomes more handlers than connections.
- **Graceful stop.** ``stop()`` stops fetching, waits for the deliveries in flight for
  ``listener.shutdown-timeout`` (10 s), then cancels the rest: a cancelled delivery is not acknowledged
  (its offset is not committed, its message goes back to the queue), unless its unit had committed
  already. The adapters are :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE` lifecycle beans, so the
  application context drains them before any ``@pre_destroy``.
- **Deliveries never inherit the transaction of the code that started the consumer**: the containers
  run their work in tasks started with :func:`pyfly.data.transaction.detached`.

Configuration, under ``pyfly.messaging.listener.*`` and ``pyfly.eda.listener.*`` (see
:meth:`ListenerContainerSettings.from_config`): ``transactional``, ``datasource``, ``shutdown-timeout``,
``concurrency``, ``retry.max-attempts``, ``retry.initial-delay``, ``retry.multiplier``,
``retry.max-delay``; and ``<prefix>.rabbitmq.prefetch``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from pyfly.config.properties.data import parse_bool, parse_float, parse_int
from pyfly.data.transaction import (
    CommitOutcomeUnknownError,
    IllegalTransactionStateError,
    Isolation,
    Propagation,
    TransactionDefinition,
    TransactionManager,
    TransactionTemplate,
    TransactionTimedOutError,
    UnitStatus,
    detached,
)
from pyfly.data.transaction.template import TransactionBoundary

if TYPE_CHECKING:
    from pyfly.core.config import Config

logger = logging.getLogger(__name__)

T = TypeVar("T")

ATTEMPT_HEADER = "x-pyfly-delivery-attempt"
"""The AMQP header that carries a republished message's delivery attempt (the first delivery is 1)."""

DEAD_LETTER_PUBLISH_ATTEMPTS = 3
"""Times an adapter tries one dead-letter publish before it gives up on this round."""

DEAD_LETTER_RETRY_DELAY = 1.0
"""The shortest wait before a delivery whose dead-letter publish failed is dead-lettered again."""


# ---------------------------------------------------------------------------------------------------------
# Back-off and retry policy
# ---------------------------------------------------------------------------------------------------------


class BackOff(Protocol):
    """How long to wait before the next attempt of a delivery that failed *failures* times."""

    def delay_after(self, failures: int) -> float:
        """Seconds to wait after the *failures*-th failure (1 for the first)."""
        ...


@dataclass(frozen=True)
class FixedBackOff:
    """The same delay after every failure."""

    delay: float

    def delay_after(self, failures: int) -> float:
        """*delay*, whatever the failure count."""
        return self.delay


@dataclass(frozen=True)
class LinearBackOff:
    """``step * failures``: what ``@message_listener(retry_delay=...)`` has always meant."""

    step: float

    def delay_after(self, failures: int) -> float:
        """*step* times the failure count."""
        return self.step * failures


@dataclass(frozen=True)
class ExponentialBackOff:
    """``initial * multiplier ** (failures - 1)``, capped at *max_delay*."""

    initial: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 30.0

    def delay_after(self, failures: int) -> float:
        """The exponential delay after the *failures*-th failure, capped."""
        return min(self.max_delay, self.initial * self.multiplier ** max(0, failures - 1))


class PoisonMessageError(Exception):
    """A delivery the adapter could not turn into what its handler takes (bytes no serializer reads).

    Attempting it again cannot help, so the container dead-letters it at once. ``cause`` is the
    conversion's exception, and the dead-letter headers name it.
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"The message cannot be read: {type(cause).__name__}: {cause}")
        self.cause = cause
        self.__cause__ = cause


def failure_cause(error: BaseException) -> BaseException:
    """The exception a dead-letter record names: the conversion failure behind a :class:`PoisonMessageError`."""
    return error.cause if isinstance(error, PoisonMessageError) else error


@dataclass(frozen=True)
class RetryPolicy:
    """How many times a delivery is attempted, and how long the container waits in between.

    *max_attempts* counts every delivery, the first included (``1`` dead-letters at the first failure).
    *not_retryable* names exception types that are dead-lettered at the first failure, because another
    attempt cannot succeed (a validation error, a missing reference); a transient failure is attempted
    again even when its type is listed (:func:`is_transient_failure`).
    """

    max_attempts: int = 5
    backoff: BackOff = field(default_factory=ExponentialBackOff)
    not_retryable: tuple[type[BaseException], ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"RetryPolicy.max_attempts must be at least 1, got {self.max_attempts}")

    def retry_delay(self, error: BaseException, attempt: int) -> float | None:
        """The delay before attempt ``attempt + 1`` of a delivery whose attempt *attempt* raised *error*, or
        ``None`` when it goes to the dead letter now."""
        if isinstance(error, PoisonMessageError) or attempt >= self.max_attempts:
            return None
        if self.not_retryable and isinstance(error, self.not_retryable) and not is_transient_failure(error):
            return None
        return max(0.0, self.backoff.delay_after(attempt))

    def with_options(self, options: ListenerOptions | None) -> RetryPolicy:
        """This policy with a listener's own attempts and back-off, where it declares them."""
        if options is None:
            return self
        return replace(
            self,
            max_attempts=self.max_attempts if options.max_attempts is None else options.max_attempts,
            backoff=self.backoff if options.backoff is None else options.backoff,
        )


# ---------------------------------------------------------------------------------------------------------
# Transient failures
# ---------------------------------------------------------------------------------------------------------

# PostgreSQL SQLSTATE classes and codes that describe the moment, not the message: connection exceptions
# (08), insufficient resources (53), operator intervention (57P01-57P03 admin/crash shutdown, cannot
# connect now), serialization failure and deadlock (40001, 40P01), statement completion unknown (40003),
# lock not available (55P03), query canceled by statement_timeout (57014), idle in transaction session
# timeout (25P03).
_TRANSIENT_SQLSTATE_CLASSES = ("08", "53", "57P")
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01", "40003", "55P03", "57014", "25P03"})

# MySQL/MariaDB error numbers: too many connections (1040), lock wait timeout (1205), deadlock (1213),
# can't connect (2002, 2003), server gone away (2006), lost connection (2013), query interrupted by
# max_execution_time (3024), client disconnected by the server (4031).
_TRANSIENT_MYSQL_ERRORS = frozenset({1040, 1205, 1213, 2002, 2003, 2006, 2013, 3024, 4031})
_MYSQL_DRIVERS = ("asyncmy", "aiomysql", "pymysql", "MySQLdb")

# SQLite and driver messages that mean "try again": the busy/locked conditions and closed connections.
_TRANSIENT_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "connection is closed",
    "connection was closed",
    "server closed the connection",
    "connection reset",
)

_TRANSIENT_MONGO_ERRORS = frozenset(
    {"AutoReconnect", "NetworkTimeout", "ConnectionFailure", "ServerSelectionTimeoutError", "ExecutionTimeout"}
)


def is_transient_failure(error: BaseException) -> bool:
    """Whether *error* (or an exception it wraps) is a failure of the moment, which another attempt can
    get past: a lost or refused connection, a pool or statement timeout, a lock, deadlock or serialization
    conflict, a transaction that timed out or whose commit outcome is unknown.

    The check reads the exception chain (``__cause__``, ``__context__`` and a driver error's ``orig``),
    the SQLSTATE of PostgreSQL drivers, the error number of MySQL drivers, the error labels of pymongo and
    SQLite's busy messages, without importing any driver.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [error]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if _transient(current):
            return True
        for attribute in ("orig", "__cause__", "__context__"):
            linked = getattr(current, attribute, None)
            if isinstance(linked, BaseException):
                stack.append(linked)
    return False


def _transient(error: BaseException) -> bool:
    if isinstance(error, (TransactionTimedOutError, CommitOutcomeUnknownError, TimeoutError, ConnectionError)):
        return True
    kind = type(error)
    module = kind.__module__ or ""
    if getattr(error, "connection_invalidated", False) is True:
        return True  # SQLAlchemy: the connection was lost and invalidated
    if module.startswith("sqlalchemy") and kind.__name__ == "TimeoutError":
        return True  # the pool timed out waiting for a connection
    sqlstate = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    if isinstance(sqlstate, str) and (
        sqlstate in _TRANSIENT_SQLSTATES or sqlstate.startswith(_TRANSIENT_SQLSTATE_CLASSES)
    ):
        return True
    if module.startswith(_MYSQL_DRIVERS) and error.args and error.args[0] in _TRANSIENT_MYSQL_ERRORS:
        return True
    has_error_label = getattr(error, "has_error_label", None)
    if callable(has_error_label) and (
        has_error_label("TransientTransactionError") or has_error_label("RetryableWriteError")
    ):
        return True
    if module.startswith("pymongo") and kind.__name__ in _TRANSIENT_MONGO_ERRORS:
        return True
    if kind.__name__ == "OperationalError":
        message = str(error).lower()
        return any(fragment in message for fragment in _TRANSIENT_MESSAGES)
    return False


# ---------------------------------------------------------------------------------------------------------
# Settings and per-listener options
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListenerOptions:
    """What one listener declares for itself (``@message_listener(retries=..., retry_delay=...,
    dead_letter_topic=...)``); ``None`` keeps the container's setting."""

    max_attempts: int | None = None
    backoff: BackOff | None = None
    dead_letter: str | None = None


class ListenerEndpoint:
    """A handler with the :class:`ListenerOptions` its listener declared, as a broker's ``subscribe`` gets
    it from the application context (see :func:`pyfly.messaging.error_handling.wrap_listener`)."""

    __slots__ = ("handler", "options")

    def __init__(self, handler: Callable[[Any], Awaitable[None]], options: ListenerOptions) -> None:
        self.handler = handler
        self.options = options

    async def __call__(self, message: Any) -> None:
        await self.handler(message)

    def __repr__(self) -> str:
        return f"ListenerEndpoint({self.handler!r}, {self.options!r})"


def listener_options(handler: object) -> ListenerOptions | None:
    """The options *handler* was subscribed with, or ``None`` for a plain handler."""
    return handler.options if isinstance(handler, ListenerEndpoint) else None


def manages_listener_errors(broker: object) -> bool:
    """Whether *broker* consumes through a listener container, which retries and dead-letters deliveries
    itself (it declares ``manages_listener_errors = True``)."""
    return getattr(broker, "manages_listener_errors", False) is True


@dataclass(frozen=True)
class ListenerContainerSettings:
    """The settings every container of one adapter shares.

    - *retry*: the default :class:`RetryPolicy` (a listener's :class:`ListenerOptions` override it);
    - *transactional*: run each delivery in a unit of work the container opens (on *datasource*, the
      default datasource when ``None``); an application without a data layer runs without one either way;
    - *shutdown_timeout*: how long ``stop()`` waits for the deliveries in flight before it cancels them;
    - *concurrency*: the most RabbitMQ deliveries one adapter runs at once (``None``: sized from the
      datasource's connection pool, one on SQLite, never more than *prefetch*);
    - *prefetch*: the RabbitMQ ``basic.qos`` prefetch count of each consumer channel;
    - *poll_timeout* and *max_poll_records*: how long one Kafka poll waits, and how many records it
      returns at most (the offsets are committed after each poll's records are done).
    """

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    transactional: bool = True
    datasource: str | None = None
    shutdown_timeout: float = 10.0
    concurrency: int | None = None
    prefetch: int = 20
    poll_timeout: float = 1.0
    max_poll_records: int = 100

    def __post_init__(self) -> None:
        if self.shutdown_timeout < 0:
            raise ValueError(f"shutdown_timeout must not be negative, got {self.shutdown_timeout}")
        if self.concurrency is not None and self.concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {self.concurrency}")
        if self.prefetch < 1:
            raise ValueError(f"prefetch must be at least 1 (0 means unbounded in AMQP), got {self.prefetch}")
        if self.max_poll_records < 1:
            raise ValueError(f"max_poll_records must be at least 1, got {self.max_poll_records}")

    @classmethod
    def from_config(cls, config: Config, prefix: str) -> ListenerContainerSettings:
        """The settings under ``<prefix>.listener.*`` (and ``<prefix>.rabbitmq.prefetch``).

        *prefix* is ``pyfly.messaging`` or ``pyfly.eda``. Keys: ``transactional`` (``true``),
        ``datasource``, ``shutdown-timeout`` (``10``), ``concurrency``, ``retry.max-attempts`` (``5``),
        ``retry.initial-delay`` (``1.0``), ``retry.multiplier`` (``2.0``), ``retry.max-delay`` (``30.0``).
        A value that does not parse raises ``ValueError`` naming the key.
        """
        base = f"{prefix}.listener"
        defaults = cls()
        default_backoff = ExponentialBackOff()

        def raw(key: str) -> Any:
            value = config.get(key)
            return None if value == "" else value

        def number(key: str, default: float) -> float:
            value = raw(f"{base}.{key}")
            return default if value is None else parse_float(value, f"{base}.{key}")

        def integer(key: str, default: int) -> int:
            value = raw(key)
            return default if value is None else parse_int(value, key)

        transactional = raw(f"{base}.transactional")
        datasource = raw(f"{base}.datasource")
        concurrency = raw(f"{base}.concurrency")
        return cls(
            retry=RetryPolicy(
                max_attempts=integer(f"{base}.retry.max-attempts", defaults.retry.max_attempts),
                backoff=ExponentialBackOff(
                    initial=number("retry.initial-delay", default_backoff.initial),
                    multiplier=number("retry.multiplier", default_backoff.multiplier),
                    max_delay=number("retry.max-delay", default_backoff.max_delay),
                ),
            ),
            transactional=(
                defaults.transactional if transactional is None else parse_bool(transactional, f"{base}.transactional")
            ),
            datasource=None if datasource is None else str(datasource),
            shutdown_timeout=number("shutdown-timeout", defaults.shutdown_timeout),
            concurrency=None if concurrency is None else parse_int(concurrency, f"{base}.concurrency"),
            prefetch=integer(f"{prefix}.rabbitmq.prefetch", defaults.prefetch),
        )


# ---------------------------------------------------------------------------------------------------------
# The container-managed unit of work
# ---------------------------------------------------------------------------------------------------------


class DeliveryState:
    """What the container knows about one attempt: whether its work committed (so it may be acknowledged
    even when a cancellation arrived right after the commit)."""

    __slots__ = ("committed",)

    def __init__(self) -> None:
        self.committed = False


_JOINING = frozenset({Propagation.REQUIRED, Propagation.MANDATORY})
"""A listener declared with one of these joins the unit the container opens, which takes its settings."""

_SHARING = frozenset({Propagation.REQUIRED, Propagation.MANDATORY, Propagation.SUPPORTS, Propagation.NESTED})
"""Beside a listener that joins the container's unit, a listener declared with one of these runs in that
unit too (``NESTED`` in a savepoint of it): it keeps its settings only when they are the unit's."""


def listener_target(listener: object) -> object:
    """The function behind *listener*, a :class:`ListenerEndpoint` and a :func:`functools.partial` looked
    through."""
    target = listener
    while True:
        if isinstance(target, ListenerEndpoint):
            target = target.handler
        elif isinstance(target, functools.partial):
            target = target.func
        else:
            return target


def transaction_definition_of(listener: object) -> TransactionDefinition | None:
    """The settings *listener*'s ``@transactional`` declares (on its method, or on its class), or ``None``."""
    definition = getattr(listener_target(listener), "__pyfly_transaction_definition__", None)
    return definition if isinstance(definition, TransactionDefinition) else None


@dataclass(frozen=True)
class _UnitSettings:
    """What a delivery's unit is opened with: *definition* (``REQUIRED``, unnamed) on *manager* (the
    :class:`TransactionManager` a listener's ``manager=`` names), else on ``definition.datasource``."""

    definition: TransactionDefinition
    manager: object = None

    @property
    def named(self) -> bool:
        """Whether a datasource or a manager is named, which must exist, rather than the default one."""
        return self.manager is not None or self.definition.datasource is not None


def _unit_settings(listener: object, definition: TransactionDefinition) -> _UnitSettings:
    """The unit *listener*'s ``@transactional`` (*definition*) would open, as the container opens it."""
    options = getattr(listener_target(listener), "__pyfly_transaction_options__", None)
    manager = options.get("manager") if isinstance(options, dict) else None
    datasource = definition.datasource
    if isinstance(manager, str):
        datasource, manager = datasource or manager, None
    normalized = replace(definition, propagation=Propagation.REQUIRED, name=None, datasource=datasource)
    return _UnitSettings(normalized, manager)


def _listener_label(listener: object) -> str:
    """*listener*'s name and ``@transactional`` settings, for logs."""
    target = listener_target(listener)
    name = getattr(target, "__qualname__", None) or repr(target)
    definition = transaction_definition_of(listener)
    if definition is None:
        return f"{name} (no @transactional)"
    parts = [f"propagation={definition.propagation.value}"]
    if definition.isolation is not Isolation.DEFAULT:
        parts.append(f"isolation={definition.isolation.value}")
    if definition.read_only:
        parts.append("read_only=True")
    if definition.timeout is not None:
        parts.append(f"timeout={definition.timeout:g}")
    if definition.datasource is not None:
        parts.append(f"datasource={definition.datasource}")
    if definition.rollback_for or definition.no_rollback_for:
        parts.append("rollback rules")
    return f"{name} ({', '.join(parts)})"


class _Plan:
    """The unit the deliveries to one set of listeners run in: *template* (``None``: none of the
    container's), and whether it names its datasource. *listeners* keeps them alive, so the ids that key
    the plan are not reused."""

    __slots__ = ("listeners", "named", "template")

    def __init__(self, listeners: tuple[object, ...], template: TransactionTemplate | None, *, named: bool) -> None:
        self.listeners = listeners
        self.template = template
        self.named = named


class ListenerInvoker:
    """Runs a delivery's handler in the unit of work the container opens for it.

    The unit takes the settings of the listeners' own ``@transactional``, which joins it, and their
    repository calls run in it; :meth:`invoke` returns only once it committed. :meth:`plan` says which
    unit a delivery gets. When no transaction manager serves the default datasource (the application has
    no data layer, or none is running), the handler runs without a unit; a datasource the settings or a
    listener name must exist.
    """

    def __init__(self, settings: ListenerContainerSettings, *, name: str) -> None:
        self._transactional = settings.transactional
        self._datasource = settings.datasource
        self._name = name
        self._template = TransactionTemplate(
            datasource=settings.datasource, propagation=Propagation.REQUIRED, name=f"listener {name}"
        )
        self._default = _UnitSettings(TransactionDefinition(datasource=settings.datasource))
        self._default_plan = _Plan((), self._template, named=settings.datasource is not None)
        self._plans: dict[tuple[int, ...], _Plan] = {}

    def manager(self) -> TransactionManager | None:
        """The transaction manager of the container's default unit now, or ``None`` when deliveries run
        without a unit."""
        if not self._transactional:
            return None
        try:
            return self._template.manager()
        except IllegalTransactionStateError:
            if self._datasource is not None:
                raise
            return None

    def validate(self) -> None:
        """Fail now, not at every delivery, when the settings name a datasource no manager serves."""
        if self._transactional and self._datasource is not None:
            self.manager()

    def plan(self, listeners: Sequence[object]) -> TransactionDefinition | None:
        """The definition of the unit a delivery to *listeners* runs in, or ``None`` when the container
        opens none and each listener runs as its own ``@transactional`` declares.

        - A listener without ``@transactional``, or declared ``REQUIRED`` or ``MANDATORY``, joins the
          delivery's unit, and the container opens it with that listener's settings: isolation, read-only,
          timeout, rollback rules and datasource (``manager=`` included). Without ``@transactional`` it is
          ``REQUIRED`` on ``listener.datasource``.
        - A listener declared ``REQUIRES_NEW``, ``NESTED``, ``SUPPORTS``, ``NOT_SUPPORTED`` or ``NEVER``
          needs no unit of the container's: when every listener of the delivery is one of those, the
          container opens none and each runs as declared, with no unit bound (``REQUIRES_NEW`` and
          ``NESTED`` begin their own). The delivery is acknowledged after they returned, so after their
          own units committed.
        - The listeners of one delivery share its unit only when each runs in it with its own settings:
          beside a listener that joins it, a ``SUPPORTS`` or ``NESTED`` listener (which would join it, or
          take a savepoint of it) must declare the same settings, and a ``REQUIRES_NEW``, ``NOT_SUPPORTED``
          or ``NEVER`` listener never does (a second unit or connection beside the delivery's, or a
          refusal). When they cannot share one, the container opens none, and logs a WARNING naming them
          the first time: each runs as its own ``@transactional`` declares (a listener without one gets a
          short unit per repository call), and a later listener's failure delivers the message again to
          the ones whose work committed.

        The plan is worked out once per set of listeners. With ``listener.transactional`` off there is
        never a unit of the container's.
        """
        found = self._plan_for(listeners)
        if not self._transactional or found.template is None:
            return None
        return found.template.definition

    def _plan_for(self, listeners: Sequence[object]) -> _Plan:
        key = tuple(id(listener) for listener in listeners)
        found = self._plans.get(key)
        if found is None:
            found = self._plans[key] = self._work_out(tuple(listeners))
        return found

    def _work_out(self, listeners: tuple[object, ...]) -> _Plan:
        declared = [(listener, transaction_definition_of(listener)) for listener in listeners]
        joining = [
            self._default if definition is None else _unit_settings(listener, definition)
            for listener, definition in declared
            if definition is None or definition.propagation in _JOINING
        ]
        if not joining:
            return _Plan(listeners, None, named=False)
        chosen = joining[0]
        misfits = [
            listener
            for listener, definition in declared
            if not (
                chosen == self._default
                if definition is None
                else definition.propagation in _SHARING and _unit_settings(listener, definition) == chosen
            )
        ]
        if misfits:
            if self._transactional:
                logger.warning(
                    "listener_units_differ container=%s listeners=%s: they cannot share one unit of work, so "
                    "the container opens none; each runs as its own @transactional declares (without one, a "
                    "short unit per repository call), and when one fails the message is delivered again to "
                    "the others as well, whose work committed",
                    self._name,
                    [_listener_label(listener) for listener in listeners],
                )
            return _Plan(listeners, None, named=False)
        if chosen == self._default:
            return _Plan(listeners, self._template, named=self._default_plan.named)
        definition = replace(chosen.definition, name=f"listener {self._name}")
        settings = {field_.name: getattr(definition, field_.name) for field_ in fields(definition) if field_.init}
        return _Plan(listeners, TransactionTemplate(chosen.manager, **settings), named=chosen.named)

    def _boundary(self, listeners: Sequence[object] | None) -> TransactionBoundary | None:
        if not self._transactional:
            return None
        found = self._default_plan if listeners is None else self._plan_for(listeners)
        if found.template is None:
            return None
        try:
            manager = found.template.manager()
        except IllegalTransactionStateError:
            if found.named:
                raise
            return None  # no data layer: a listener's own @transactional fails as it would anyway
        return TransactionBoundary(manager, found.template.definition)

    async def invoke(
        self, call: Callable[[], Awaitable[None]], state: DeliveryState, listeners: Sequence[object] | None = None
    ) -> None:
        """Await *call* inside the unit a delivery to *listeners* runs in (see :meth:`plan`; the container's
        default unit when ``None``); *state* records whether that unit committed."""
        boundary = self._boundary(listeners)
        if boundary is None:
            await call()
            state.committed = True
            return
        unit = None
        try:
            async with boundary as unit:
                await call()
        finally:
            if unit is not None and unit.status is UnitStatus.COMMITTED:
                state.committed = True
        state.committed = True

    def suggested_concurrency(self, ceiling: int) -> int:
        """How many deliveries should run at once: the datasource's pool size (one on SQLite, which has a
        single writer), never more than *ceiling*; *ceiling* when there is no pool to size from."""
        try:
            manager = self.manager()
        except IllegalTransactionStateError:
            return ceiling
        if manager is None:
            return ceiling
        if manager.capabilities.backend == "sqlite":
            return 1
        size = _pool_size(manager)
        return max(1, min(ceiling, size)) if size else ceiling


def _pool_size(manager: object) -> int | None:
    """The size of *manager*'s connection pool, when its engine has a sized pool (not NullPool)."""
    try:
        pool = getattr(getattr(manager, "engine", None), "pool", None)
        size = getattr(pool, "size", None)
        value = size() if callable(size) else None
    except Exception:  # noqa: BLE001 — a manager without an engine has nothing to size from
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class ConcurrencyLimit:
    """The most deliveries an adapter's consumers run at once, shared by all of them.

    The size is worked out at the first delivery (the transaction managers are installed by then): the
    settings' ``concurrency``, else :meth:`ListenerInvoker.suggested_concurrency`.
    """

    def __init__(self, size: Callable[[], int]) -> None:
        self._size_of = size
        self._semaphore: asyncio.Semaphore | None = None
        self.size: int | None = None

    async def __aenter__(self) -> None:
        if self._semaphore is None:
            self.size = max(1, self._size_of())
            self._semaphore = asyncio.Semaphore(self.size)
        await self._semaphore.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        assert self._semaphore is not None
        self._semaphore.release()


def dead_letter_headers(
    *,
    topic: str,
    error: BaseException,
    attempts: int,
    partition: int | None = None,
    offset: int | None = None,
    queue: str | None = None,
) -> dict[str, str]:
    """The headers a dead-letter record carries on both brokers.

    ``x-original-topic`` and ``x-exception`` (what ``@message_listener`` dead letters always carried),
    ``x-exception-message``, ``x-dlt-attempts``, and the origin: ``x-dlt-reason``,
    ``x-dlt-source-topic`` plus ``x-dlt-source-partition``/``x-dlt-source-offset`` on Kafka and
    ``x-dlt-source-queue`` on RabbitMQ.
    """
    cause = failure_cause(error)
    headers = {
        "x-original-topic": topic,
        "x-exception": type(cause).__name__,
        "x-exception-message": str(cause)[:1000],
        "x-dlt-reason": type(cause).__name__,
        "x-dlt-source-topic": topic,
        "x-dlt-attempts": str(attempts),
    }
    if partition is not None:
        headers["x-dlt-source-partition"] = str(partition)
    if offset is not None:
        headers["x-dlt-source-offset"] = str(offset)
    if queue is not None:
        headers["x-dlt-source-queue"] = queue
    return headers


def _cancel_requested() -> bool:
    """Whether the running task was asked to cancel (a stop), as opposed to a ``CancelledError`` a handler
    raised on its own (awaiting a task that was cancelled), which is the delivery's failure."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _wait_or_cancel(tasks: set[asyncio.Task[Any]], timeout: float, *, container: str) -> None:
    """Wait for *tasks* up to *timeout*, then cancel the ones still running and wait for them to end."""
    if not tasks:
        return
    try:
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                "listener_stop_timeout container=%s in_flight=%d timeout_s=%s: cancelling; a cancelled delivery "
                "is not acknowledged and is delivered again",
                container,
                len(pending),
                timeout,
            )
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=max(timeout, 1.0))
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise
    for task in tasks:
        if task.done() and not task.cancelled() and task.exception() is not None:
            logger.error("listener_task_failed container=%s", container, exc_info=task.exception())


# ---------------------------------------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------------------------------------

KafkaDeadLetter = Callable[[Any, BaseException, int], Awaitable[bool | None]]
"""Publishes a record to its dead-letter topic: ``(record, error, attempts)``; raising keeps it unacknowledged,
and returning ``False`` says it kept no copy (the record is logged as skipped)."""

ListenerLookup = Callable[[Any], Sequence[object]]
"""The listeners a delivery's payload reaches, whose ``@transactional`` settings shape its unit of work."""


class KafkaListenerContainer(Generic[T]):
    """Consumes *topics* in one Kafka consumer and runs each record through the container's guarantees
    (see the module documentation).

    - *consumer_factory* is called with ``group_id``, ``enable_auto_commit=False`` and
      ``auto_offset_reset`` and returns an unstarted ``AIOKafkaConsumer`` (or one with its API);
    - *convert* turns a record and its attempt number into what *handler* takes, outside the unit; an
      exception there makes the record a :class:`PoisonMessageError`;
    - *handler* runs inside the delivery's unit of work;
    - *listeners* returns the listeners a payload reaches (``None``: *handler* itself), whose
      ``@transactional`` settings the delivery's unit takes (see :meth:`ListenerInvoker.plan`);
    - *dead_letter* publishes a record that ran out of attempts; ``None`` logs it and skips it;
    - *ready*: the loop fetches nothing until this event is set (a bus sets it once a handler
      subscribed); ``None`` fetches from the start.

    Offsets are committed after each poll's records are done, and before a partition is sought back, and
    when partitions are revoked, and at stop: always the offset after the last record whose unit
    committed (or that was dead-lettered), never past one still running or cancelled. Without a consumer
    group nothing is committed, so a restart does not deliver anything again: the container warns.
    """

    def __init__(
        self,
        *,
        topics: Sequence[str],
        group: str | None,
        consumer_factory: Callable[..., Any],
        convert: Callable[[Any, int], T],
        handler: Callable[[T], Awaitable[None]],
        dead_letter: KafkaDeadLetter | None,
        settings: ListenerContainerSettings,
        retry: RetryPolicy | None = None,
        auto_offset_reset: str = "latest",
        name: str | None = None,
        listeners: ListenerLookup | None = None,
        ready: asyncio.Event | None = None,
    ) -> None:
        self._topics = list(topics)
        self._group = group
        self._consumer_factory = consumer_factory
        self._convert = convert
        self._handler = handler
        self._dead_letter = dead_letter
        self._settings = settings
        self._policy = retry or settings.retry
        self._auto_offset_reset = auto_offset_reset
        self.name = name or ",".join(self._topics)
        self._listeners = listeners
        self._ready = ready
        self._invoker = ListenerInvoker(settings, name=self.name)
        self._consumer: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        #: Cleared while a record is in flight: a stop cancels the loop only when it is set.
        self._idle = asyncio.Event()
        self._idle.set()
        #: Partitions this member owns (``None`` without a group: everything it fetches).
        self._assigned: set[Any] | None = None
        #: Partitions a rebalance is taking away: their records are not started any more.
        self._revoking: set[Any] = set()
        #: The next offset to commit, by partition: one past the last record that is done.
        self._pending: dict[Any, int] = {}
        #: Failed attempts of the record a partition was sought back to.
        self._failures: dict[tuple[Any, int], int] = {}
        #: Records out of attempts whose dead-letter publish failed: (error, attempts).
        self._recovering: dict[tuple[Any, int], tuple[BaseException, int]] = {}
        self._recover_rounds: dict[tuple[Any, int], int] = {}
        #: Paused partitions and the loop time at which they resume.
        self._paused_until: dict[Any, float] = {}

    @property
    def topics(self) -> list[str]:
        """The topics this container consumes."""
        return list(self._topics)

    @property
    def group(self) -> str | None:
        """The consumer group (``None``: an isolated consumer that commits nothing)."""
        return self._group

    @property
    def running(self) -> bool:
        """Whether the consume loop is running."""
        return self._task is not None and not self._task.done()

    def check_listeners(self, listeners: Sequence[object]) -> None:
        """Work out now the unit of work the deliveries to *listeners* run in, so that listeners that
        cannot share one are logged at subscription rather than at the first delivery."""
        self._invoker.plan(listeners)

    async def start(self) -> None:
        """Create, subscribe and start the consumer, then the consume loop (in a detached task)."""
        if self._task is not None:
            return
        self._invoker.validate()
        consumer = self._consumer_factory(
            group_id=self._group, enable_auto_commit=False, auto_offset_reset=self._auto_offset_reset
        )
        if self._group is not None:
            self._assigned = set()
            consumer.subscribe(self._topics, listener=_kafka_rebalance_listener(self._on_revoked, self._on_assigned))
        else:
            self._assigned = None
            consumer.subscribe(self._topics)
            logger.warning(
                "listener_without_consumer_group topics=%s: offsets are not committed, so a restart does not "
                "deliver a failed or unfinished record again; give the listener a group",
                self._topics,
            )
        self._consumer = consumer
        self._stopping = False
        try:
            await consumer.start()
        except BaseException:
            self._consumer = None
            with contextlib.suppress(Exception):
                await consumer.stop()
            raise
        self._task = detached(self._run(consumer), name=f"pyfly-kafka-listener[{self.name}]")

    async def stop(self) -> None:
        """Stop fetching, wait for the record in flight (up to the shutdown timeout, then cancel it without
        committing its offset), commit what is done and close the consumer."""
        task, consumer = self._task, self._consumer
        self._stopping = True
        if task is not None:
            if self._idle.is_set():
                task.cancel()  # nothing is in flight: the fetched records are simply not processed
            await _wait_or_cancel({task}, self._settings.shutdown_timeout, container=self.name)
        self._task = None
        if consumer is not None:
            try:
                await self._commit_pending(consumer)
            finally:
                self._consumer = None
                with contextlib.suppress(Exception):
                    await consumer.stop()

    # -- the consume loop ------------------------------------------------------------------------------

    async def _run(self, consumer: Any) -> None:
        loop = asyncio.get_running_loop()
        if self._ready is not None:
            await self._ready.wait()  # nothing is in flight: a stop cancels the wait
        while not self._stopping:
            self._resume_due(consumer, loop.time())
            try:
                batch = await consumer.getmany(
                    timeout_ms=self._poll_timeout_ms(loop.time()), max_records=self._settings.max_poll_records
                )
            except Exception as error:
                if type(error).__name__ == "ConsumerStoppedError":
                    return
                logger.warning("listener_poll_failed container=%s: %s", self.name, error)
                await asyncio.sleep(1.0)
                continue
            for tp, records in batch.items():
                for record in records:
                    if self._stopping or not self._owns(tp):
                        break
                    self._idle.clear()
                    try:
                        keep_going = await self._deliver(consumer, tp, record)
                    except Exception:
                        # Not the handler's failure (the container handles those): its own. Log it, and
                        # leave the record uncommitted, to be fetched again, rather than stop consuming.
                        logger.exception(
                            "listener_delivery_error container=%s topic=%s partition=%s offset=%s",
                            self.name,
                            record.topic,
                            record.partition,
                            record.offset,
                        )
                        self._back_off(consumer, tp, record.offset, DEAD_LETTER_RETRY_DELAY)
                        keep_going = False
                    finally:
                        self._idle.set()
                    if not keep_going:
                        break  # the partition was sought back: its later records come again
            await self._commit_pending(consumer)

    async def _deliver(self, consumer: Any, tp: Any, record: Any) -> bool:
        """Process one record; ``False`` when its partition was sought back to it."""
        key = (tp, record.offset)
        recovering = self._recovering.get(key)
        if recovering is not None:
            return await self._recover(consumer, tp, record, *recovering)
        attempt = self._failures.get(key, 0) + 1
        try:
            payload = self._convert(record, attempt)
        except Exception as unreadable:
            return await self._recover(consumer, tp, record, PoisonMessageError(unreadable), attempt)
        state = DeliveryState()
        error: BaseException
        try:
            await self._invoker.invoke(functools.partial(self._handler, payload), state, self._targets(payload))
        except asyncio.CancelledError as cancelled:
            if _cancel_requested():
                if state.committed:
                    self._completed(tp, record.offset)
                raise
            error = cancelled  # the handler's own, not a stop: the delivery failed
        except Exception as failed:
            error = failed
        else:
            self._completed(tp, record.offset)
            return True
        delay = self._policy.retry_delay(error, attempt)
        if delay is None:
            return await self._recover(consumer, tp, record, error, attempt)
        self._failures[key] = attempt
        logger.warning(
            "listener_delivery_failed container=%s topic=%s partition=%s offset=%s attempt=%d/%d "
            "retry_in_s=%.3f transient=%s: %r",
            self.name,
            record.topic,
            record.partition,
            record.offset,
            attempt,
            self._policy.max_attempts,
            delay,
            is_transient_failure(error),
            error,
        )
        self._back_off(consumer, tp, record.offset, delay)
        return False

    def _targets(self, payload: T) -> Sequence[object]:
        return self._listeners(payload) if self._listeners is not None else (self._handler,)

    async def _recover(self, consumer: Any, tp: Any, record: Any, error: BaseException, attempts: int) -> bool:
        """Dead-letter a record that ran out of attempts, then count it done; ``False`` when the publish failed
        and the partition was sought back to try again."""
        key = (tp, record.offset)
        if self._dead_letter is None:
            logger.error(
                "listener_delivery_skipped container=%s topic=%s partition=%s offset=%s attempts=%d: "
                "dead-lettering is off, the record is skipped",
                self.name,
                record.topic,
                record.partition,
                record.offset,
                attempts,
                exc_info=(type(error), error, error.__traceback__),
            )
            self._completed(tp, record.offset)
            return True
        failure: BaseException | None = None
        kept: bool | None = None
        try:
            kept = await self._dead_letter(record, error, attempts)
        except asyncio.CancelledError as cancelled:
            if _cancel_requested():
                self._recovering[key] = (error, attempts)
                raise
            failure = cancelled
        except Exception as failed:
            failure = failed
        if failure is not None:
            self._recovering[key] = (error, attempts)
            rounds = self._recover_rounds.get(key, 0) + 1
            self._recover_rounds[key] = rounds
            delay = max(DEAD_LETTER_RETRY_DELAY, self._policy.backoff.delay_after(rounds))
            logger.error(
                "listener_dead_letter_failed container=%s topic=%s partition=%s offset=%s: %r; the record stays "
                "uncommitted and is dead-lettered again in %.1f s",
                self.name,
                record.topic,
                record.partition,
                record.offset,
                failure,
                delay,
            )
            self._back_off(consumer, tp, record.offset, delay)
            return False
        if kept is False:
            logger.error(
                "listener_delivery_skipped container=%s topic=%s partition=%s offset=%s attempts=%d: no "
                "dead-letter destination kept a copy, the record is skipped",
                self.name,
                record.topic,
                record.partition,
                record.offset,
                attempts,
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            logger.warning(
                "listener_delivery_dead_lettered container=%s topic=%s partition=%s offset=%s attempts=%d reason=%s",
                self.name,
                record.topic,
                record.partition,
                record.offset,
                attempts,
                type(failure_cause(error)).__name__,
            )
        self._completed(tp, record.offset)
        return True

    def _completed(self, tp: Any, offset: int) -> None:
        self._pending[tp] = offset + 1
        key = (tp, offset)
        self._failures.pop(key, None)
        self._recovering.pop(key, None)
        self._recover_rounds.pop(key, None)

    def _back_off(self, consumer: Any, tp: Any, offset: int, delay: float) -> None:
        """Seek *tp* back to *offset* and pause it for *delay* seconds: the record is fetched again then,
        and the records after it keep their order.

        A partition a rebalance took away meanwhile cannot be sought: the record stays uncommitted, and the
        partition's new owner gets it from the committed offset.
        """
        try:
            consumer.seek(tp, offset)
            if delay > 0:
                consumer.pause(tp)
        except Exception as error:  # noqa: BLE001 — the partition is no longer this member's
            logger.warning(
                "listener_back_off_failed container=%s partition=%s offset=%s: %s; its new owner delivers the "
                "record again from the committed offset",
                self.name,
                tp,
                offset,
                error,
            )
            return
        if delay > 0:
            self._paused_until[tp] = asyncio.get_running_loop().time() + delay

    def _resume_due(self, consumer: Any, now: float) -> None:
        for tp in [tp for tp, until in self._paused_until.items() if until <= now]:
            del self._paused_until[tp]
            if self._assigned_here(tp):
                with contextlib.suppress(Exception):
                    consumer.resume(tp)

    def _poll_timeout_ms(self, now: float) -> int:
        timeout = self._settings.poll_timeout
        if self._paused_until:
            timeout = min(timeout, max(0.0, min(self._paused_until.values()) - now))
        return int(timeout * 1000)

    def _owns(self, tp: Any) -> bool:
        """Whether a record of *tp* may be started: the partition is this member's and no rebalance is
        taking it away."""
        return tp not in self._revoking and self._assigned_here(tp)

    def _assigned_here(self, tp: Any) -> bool:
        """Whether *tp* is still assigned to this member (its done offsets may be committed)."""
        return self._assigned is None or tp in self._assigned

    async def _commit_pending(self, consumer: Any) -> None:
        """Commit the offsets of the records that are done (only on partitions this member still owns)."""
        if not self._pending:
            return
        if self._group is None:
            self._pending.clear()
            return
        for tp in [tp for tp in self._pending if not self._assigned_here(tp)]:
            del self._pending[tp]
        offsets = dict(self._pending)
        if not offsets:
            return
        try:
            await consumer.commit(offsets)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "listener_commit_failed container=%s offsets=%s: %s; the records after the last committed "
                "offset are delivered again",
                self.name,
                {f"{tp.topic}-{tp.partition}": offset for tp, offset in offsets.items()},
                error,
            )
            return
        for tp, offset in offsets.items():
            if self._pending.get(tp) == offset:
                del self._pending[tp]

    # -- rebalancing -----------------------------------------------------------------------------------

    async def _on_revoked(self, revoked: set[Any]) -> None:
        """Before the group takes partitions away: start none of their records any more, let the record in
        flight finish (bounded), commit what is done on them, and forget their retry state."""
        self._revoking |= revoked
        try:
            if not self._idle.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._idle.wait(), timeout=self._settings.shutdown_timeout)
            consumer = self._consumer
            offsets = {tp: offset for tp, offset in self._pending.items() if tp in revoked}
            if offsets and consumer is not None:
                try:
                    await consumer.commit(offsets)
                except Exception as error:  # noqa: BLE001 — the new owner delivers those records again
                    logger.warning("listener_commit_on_revoke_failed container=%s: %s", self.name, error)
            for tp in revoked:
                self._pending.pop(tp, None)
                self._paused_until.pop(tp, None)
            for stale in (self._failures, self._recovering, self._recover_rounds):
                for key in [key for key in stale if key[0] in revoked]:
                    del stale[key]
            if self._assigned is not None:
                self._assigned -= revoked
        finally:
            self._revoking -= revoked

    def _on_assigned(self, assigned: set[Any]) -> None:
        self._assigned = set(assigned)


def _kafka_rebalance_listener(
    on_revoked: Callable[[set[Any]], Awaitable[None]], on_assigned: Callable[[set[Any]], None]
) -> Any:
    """An aiokafka ``ConsumerRebalanceListener`` that calls the container back."""
    from aiokafka import ConsumerRebalanceListener  # type: ignore[import-untyped]

    class _RebalanceListener(ConsumerRebalanceListener):  # type: ignore[misc]
        async def on_partitions_revoked(self, revoked: Any) -> None:
            await on_revoked(set(revoked))

        async def on_partitions_assigned(self, assigned: Any) -> None:
            on_assigned(set(assigned))

    return _RebalanceListener()


# ---------------------------------------------------------------------------------------------------------
# RabbitMQ
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RabbitDeadLetter:
    """Where a RabbitMQ delivery that ran out of attempts goes: *exchange* (declared direct and durable)
    with *routing_key*, and *queue* (declared durable and bound to it) when the adapter owns the queue
    that keeps them."""

    exchange: str
    routing_key: str
    queue: str | None = None


RabbitDeadLetterHook = Callable[[Any, BaseException, int], Awaitable[None]]
"""Called after a message was dead-lettered, before it is acked: ``(message, error, attempts)``."""


def delivery_attempt(message: Any) -> int:
    """The attempt number of an AMQP delivery, from its ``x-pyfly-delivery-attempt`` header (1 without)."""
    headers = getattr(message, "headers", None) or {}
    raw = headers.get(ATTEMPT_HEADER) if isinstance(headers, Mapping) else None
    try:
        attempt = int(raw.decode() if isinstance(raw, bytes) else raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1
    return max(1, attempt)


class RabbitListenerContainer(Generic[T]):
    """Consumes one queue on a channel of its own and runs each delivery through the container's
    guarantees (see the module documentation).

    - *connection* is an open ``aio_pika`` connection (robust or not);
    - *queue* is declared durable, bound to each ``(exchange, routing_key)`` of *bindings*;
    - *convert* turns a delivery and its attempt number into what *handler* takes, outside the unit; an
      exception there makes it a :class:`PoisonMessageError`;
    - *dead_letters* are tried in order for a delivery that ran out of attempts: the first publish the
      broker routes wins; with none, the message is logged and rejected without requeue;
    - *after_dead_letter* runs once the copy is in a dead-letter queue (a store that records it); its
      failure is logged and the message acked all the same, since the copy is safe;
    - *limit* is the adapter's :class:`ConcurrencyLimit`, shared by its consumers;
    - *listeners* returns the listeners a payload reaches (``None``: *handler* itself), whose
      ``@transactional`` settings the delivery's unit takes (see :meth:`ListenerInvoker.plan`);
    - *ready*: the queue and its routes are declared at :meth:`start`, but consuming begins only once this
      event is set (a bus sets it once a handler subscribed); ``None`` consumes from the start.

    The consumer channel has publisher confirms and ``on_return_raises``: a republished or dead-lettered
    copy that no queue takes raises instead of vanishing, and the original stays unacknowledged.
    """

    def __init__(
        self,
        *,
        connection: Any,
        queue: str,
        bindings: Sequence[tuple[str, str]],
        convert: Callable[[Any, int], T],
        handler: Callable[[T], Awaitable[None]],
        dead_letters: Sequence[RabbitDeadLetter],
        settings: ListenerContainerSettings,
        limit: ConcurrencyLimit,
        retry: RetryPolicy | None = None,
        after_dead_letter: RabbitDeadLetterHook | None = None,
        name: str | None = None,
        listeners: ListenerLookup | None = None,
        ready: asyncio.Event | None = None,
    ) -> None:
        self._connection = connection
        self._queue_name = queue
        self._bindings = list(bindings)
        self._convert = convert
        self._handler = handler
        self._dead_letters = list(dead_letters)
        self._settings = settings
        self._limit = limit
        self._policy = retry or settings.retry
        self._after_dead_letter = after_dead_letter
        self.name = name or queue
        self._listeners = listeners
        self._ready = ready
        self._invoker = ListenerInvoker(settings, name=self.name)
        self._consuming: asyncio.Task[None] | None = None
        self._channel: Any = None
        self._queue: Any = None
        self._exchanges: dict[str, Any] = {}
        self._tag: Any = None
        self._stopping = False
        self._stopped = asyncio.Event()
        self._in_flight: set[asyncio.Task[None]] = set()

    @property
    def queue(self) -> str:
        """The name of the queue this container consumes."""
        return self._queue_name

    async def start(self) -> None:
        """Open the consumer channel, set its prefetch, declare the queue, its bindings and the dead-letter
        routes, and start consuming with manual acknowledgement (once *ready* is set)."""
        if self._channel is not None:
            return
        import aio_pika

        self._invoker.validate()
        self._stopping = False
        self._stopped.clear()
        channel = await self._connection.channel(publisher_confirms=True, on_return_raises=True)
        try:
            await channel.set_qos(prefetch_count=self._settings.prefetch)
            queue = await channel.declare_queue(self._queue_name, durable=True)
            for exchange_name, routing_key in self._bindings:
                exchange = await self._exchange(channel, exchange_name, aio_pika.ExchangeType.DIRECT)
                await queue.bind(exchange, routing_key=routing_key)
            for route in self._dead_letters:
                exchange = await self._exchange(channel, route.exchange, aio_pika.ExchangeType.DIRECT)
                if route.queue is not None:
                    dead = await channel.declare_queue(route.queue, durable=True)
                    await dead.bind(exchange, routing_key=route.routing_key)
            self._channel, self._queue = channel, queue
            if self._ready is None or self._ready.is_set():
                self._tag = await queue.consume(self._on_message, no_ack=False)
            else:
                self._consuming = detached(
                    self._consume_when_ready(self._ready, queue), name=f"pyfly-rabbit-listener-ready[{self.name}]"
                )
        except BaseException:
            self._channel = self._queue = None
            with contextlib.suppress(Exception):
                await channel.close()
            raise

    async def _consume_when_ready(self, ready: asyncio.Event, queue: Any) -> None:
        await ready.wait()
        if self._stopping or self._queue is not queue:
            return
        try:
            self._tag = await queue.consume(self._on_message, no_ack=False)
        except Exception:
            logger.exception("listener_consume_failed container=%s queue=%s", self.name, self._queue_name)

    async def _exchange(self, channel: Any, name: str, kind: Any) -> Any:
        exchange = self._exchanges.get(name)
        if exchange is None:
            exchange = await channel.declare_exchange(name, kind, durable=True)
            self._exchanges[name] = exchange
        return exchange

    async def stop(self) -> None:
        """Cancel the consumer (no new deliveries), wait for the deliveries in flight (up to the shutdown
        timeout, then cancel them: a cancelled delivery is requeued), and close the channel (the broker
        requeues whatever is still unacknowledged)."""
        self._stopping = True
        self._stopped.set()
        consuming, self._consuming = self._consuming, None
        if consuming is not None and not consuming.done():
            consuming.cancel()
            with contextlib.suppress(BaseException):
                await consuming
        channel, queue, tag = self._channel, self._queue, self._tag
        self._tag = None
        if queue is not None and tag is not None:
            with contextlib.suppress(Exception):
                await queue.cancel(tag)
        await _wait_or_cancel(set(self._in_flight), self._settings.shutdown_timeout, container=self.name)
        self._channel = self._queue = None
        self._exchanges.clear()
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.close()

    # -- deliveries ------------------------------------------------------------------------------------

    async def _on_message(self, message: Any) -> None:
        """aio-pika's consumer callback: hand the delivery to a detached task of its own."""
        if self._stopping:
            await self._release(message)
            return
        task = detached(self._deliver_safely(message), name=f"pyfly-rabbit-listener[{self.name}]")
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _deliver_safely(self, message: Any) -> None:
        """:meth:`_deliver`, with the container's own failures logged and the message requeued, so a
        delivery never stays unacknowledged until its channel closes."""
        try:
            await self._deliver(message)
        except Exception:
            logger.exception("listener_delivery_error container=%s queue=%s", self.name, self._queue_name)
            if not self._stopping:  # not at once: a failure of the container's own must not spin
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=DEAD_LETTER_RETRY_DELAY)
            await self._release(message)

    async def _deliver(self, message: Any) -> None:
        attempt = delivery_attempt(message)
        try:
            payload = self._convert(message, attempt)
        except Exception as error:
            await self._dead_letter(message, PoisonMessageError(error), attempt)
            return
        failure: BaseException | None = None
        async with self._limit:
            if self._stopping:
                await self._release(message)
                return
            state = DeliveryState()
            try:
                await self._invoker.invoke(functools.partial(self._handler, payload), state, self._targets(payload))
            except asyncio.CancelledError as cancelled:
                if _cancel_requested():
                    await _shielded(self._ack(message) if state.committed else self._release(message))
                    raise
                failure = cancelled  # the handler's own, not a stop: the delivery failed
            except Exception as error:
                failure = error
            else:
                await self._ack(message)
                return
        assert failure is not None
        delay = self._policy.retry_delay(failure, attempt)
        if delay is None:
            await self._dead_letter(message, failure, attempt)
            return
        logger.warning(
            "listener_delivery_failed container=%s queue=%s attempt=%d/%d retry_in_s=%.3f transient=%s: %r",
            self.name,
            self._queue_name,
            attempt,
            self._policy.max_attempts,
            delay,
            is_transient_failure(failure),
            failure,
        )
        await self._retry_later(message, attempt, delay)

    def _targets(self, payload: T) -> Sequence[object]:
        return self._listeners(payload) if self._listeners is not None else (self._handler,)

    async def _retry_later(self, message: Any, attempt: int, delay: float) -> None:
        """Hold the message for *delay* (a stop cuts the wait short), then republish it to its queue with
        the next attempt number and ack the original."""
        if delay > 0 and not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
        headers = {**(message.headers or {}), ATTEMPT_HEADER: attempt + 1}
        try:
            await self._channel.default_exchange.publish(_copy(message, headers), routing_key=self._queue_name)
        except asyncio.CancelledError:
            await _shielded(self._release(message))
            raise
        except Exception as error:
            logger.error(
                "listener_retry_publish_failed container=%s queue=%s: %s; the message is requeued",
                self.name,
                self._queue_name,
                error,
            )
            await self._release(message)
            return
        await self._ack(message)

    async def _dead_letter(self, message: Any, error: BaseException, attempts: int) -> None:
        """Publish a copy to the first dead-letter route that takes it, then ack; requeue when none does."""
        if not self._dead_letters:
            logger.error(
                "listener_delivery_dropped container=%s queue=%s attempts=%d: no dead-letter route, the message "
                "is rejected",
                self.name,
                self._queue_name,
                attempts,
                exc_info=(type(error), error, error.__traceback__),
            )
            await self._reject(message)
            return
        topic = self._bindings[0][1] if self._bindings else self._queue_name
        extra = dead_letter_headers(topic=topic, error=error, attempts=attempts, queue=self._queue_name)
        headers = {
            **{key: value for key, value in (message.headers or {}).items() if key != ATTEMPT_HEADER},
            **extra,
        }
        last: Exception | None = None
        for route in self._dead_letters:
            try:
                await self._exchanges[route.exchange].publish(_copy(message, headers), routing_key=route.routing_key)
            except asyncio.CancelledError:
                await _shielded(self._release(message))
                raise
            except Exception as failure:  # noqa: BLE001 — unroutable or refused: try the next route
                last = failure
                continue
            if self._after_dead_letter is not None:
                try:
                    await self._after_dead_letter(message, error, attempts)
                except asyncio.CancelledError:
                    await _shielded(self._release(message))
                    raise
                except Exception:  # noqa: BLE001 — the copy is in the dead-letter queue: ack all the same
                    logger.exception(
                        "listener_after_dead_letter_failed container=%s queue=%s exchange=%s routing_key=%s: the "
                        "message is dead-lettered and acked all the same",
                        self.name,
                        self._queue_name,
                        route.exchange,
                        route.routing_key,
                    )
            logger.warning(
                "listener_delivery_dead_lettered container=%s queue=%s exchange=%s routing_key=%s attempts=%d "
                "reason=%s",
                self.name,
                self._queue_name,
                route.exchange,
                route.routing_key,
                attempts,
                type(failure_cause(error)).__name__,
            )
            await self._ack(message)
            return
        logger.error(
            "listener_dead_letter_failed container=%s queue=%s: %s; the message is requeued",
            self.name,
            self._queue_name,
            last,
        )
        if not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=DEAD_LETTER_RETRY_DELAY)
        await self._release(message)

    async def _ack(self, message: Any) -> None:
        try:
            await message.ack()
        except Exception as error:  # noqa: BLE001 — a closed channel: the broker delivers it again
            logger.warning("listener_ack_failed container=%s: %s; the broker delivers it again", self.name, error)

    async def _release(self, message: Any) -> None:
        """Give the message back to its queue (``basic.reject`` with requeue)."""
        try:
            await message.reject(requeue=True)
        except Exception as error:  # noqa: BLE001 — a closed channel requeues it anyway
            logger.debug("listener_requeue_failed container=%s: %s", self.name, error)

    async def _reject(self, message: Any) -> None:
        try:
            await message.reject(requeue=False)
        except Exception as error:  # noqa: BLE001 — a closed channel requeues it, nothing is lost
            logger.debug("listener_reject_failed container=%s: %s", self.name, error)


def _copy(message: Any, headers: dict[str, Any]) -> Any:
    """A persistent copy of an incoming AMQP message with *headers*, its other properties kept."""
    import aio_pika

    return aio_pika.Message(
        body=message.body,
        headers=headers,
        content_type=getattr(message, "content_type", None),
        content_encoding=getattr(message, "content_encoding", None),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        priority=getattr(message, "priority", None),
        correlation_id=getattr(message, "correlation_id", None),
        reply_to=getattr(message, "reply_to", None),
        message_id=getattr(message, "message_id", None),
        timestamp=getattr(message, "timestamp", None),
        type=getattr(message, "type", None),
        app_id=getattr(message, "app_id", None),
    )


async def _shielded(operation: Awaitable[None]) -> None:
    """Run an acknowledgement to completion even while the delivery is being cancelled."""
    task = asyncio.ensure_future(operation)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await task


__all__ = [
    "ATTEMPT_HEADER",
    "BackOff",
    "ConcurrencyLimit",
    "DeliveryState",
    "ExponentialBackOff",
    "FixedBackOff",
    "KafkaDeadLetter",
    "KafkaListenerContainer",
    "LinearBackOff",
    "ListenerContainerSettings",
    "ListenerEndpoint",
    "ListenerInvoker",
    "ListenerLookup",
    "ListenerOptions",
    "PoisonMessageError",
    "RabbitDeadLetter",
    "RabbitListenerContainer",
    "RetryPolicy",
    "dead_letter_headers",
    "delivery_attempt",
    "failure_cause",
    "is_transient_failure",
    "listener_options",
    "listener_target",
    "manages_listener_errors",
    "transaction_definition_of",
]
