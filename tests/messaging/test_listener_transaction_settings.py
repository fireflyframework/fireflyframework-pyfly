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
"""The unit a delivery runs in takes the settings of the listener's own ``@transactional``.

The container opens a unit around every delivery, and the listener's ``@transactional`` joins it. A
participant's isolation, timeout and read-only flag do not apply to the unit it joins, so a container
unit with the default settings silently ran a ``SERIALIZABLE`` listener at the database's default level,
never timed out a listener with a ``timeout``, let a ``read_only`` listener write, failed every delivery to
a ``Propagation.NEVER`` listener, and gave a ``REQUIRES_NEW`` listener a second unit (on SQLite, a write
that waits for the lock its own delivery holds). These tests run a real application (``@message_listener``
+ ``@transactional`` + a repository on a SQLite file database) on the in-memory Kafka cluster that keeps
committed offsets, and check each setting reaches the database unit.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from pyfly.container.stereotypes import service
from pyfly.data.transaction import (
    Isolation,
    Propagation,
    TransactionDefinition,
    UnitOfWork,
    current_unit_of_work,
)
from pyfly.data.transactional import transactional
from pyfly.messaging.adapters.kafka import KafkaAdapter
from pyfly.messaging.decorators import message_listener
from pyfly.messaging.listener_container import (
    FixedBackOff,
    ListenerContainerSettings,
    ListenerEndpoint,
    ListenerInvoker,
    ListenerOptions,
    RetryPolicy,
    transaction_definition_of,
)
from pyfly.messaging.types import Message
from pyfly.resilience.retry import retry
from tests.messaging.brokers import FakeKafkaCluster
from tests.messaging.listener_app import (
    Delivered,
    DeliveredRepository,
    boot,
    broker_bean,
    committed_bodies,
    eventually,
)
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend

TOPIC = "orders"
GROUP = "order-service"

Handler = Callable[[DeliveredRepository, Message], Awaitable[None]]


def _adapter(cluster: FakeKafkaCluster) -> KafkaAdapter:
    retry = RetryPolicy(max_attempts=3, backoff=FixedBackOff(0.01))
    settings = ListenerContainerSettings(retry=retry, poll_timeout=0.05)
    return KafkaAdapter(
        "fake:9092",
        settings=settings,
        auto_offset_reset="earliest",
        consumer_factory=cluster.consumer,
        producer_factory=cluster.producer,
    )


def _listener(handle: Handler, **options: Any) -> type:
    """A ``@service`` whose ``@message_listener`` method is ``@transactional(**options)``."""

    @service
    class TransactionalListener:
        def __init__(self, repo: DeliveredRepository) -> None:
            self.repo = repo

        @message_listener(TOPIC, group=GROUP)
        @transactional(**options)
        async def on_message(self, message: Message) -> None:
            await handle(self.repo, message)

    return TransactionalListener


async def _run(relational_backend: RelationalBackend, cluster: FakeKafkaCluster, *beans: type, what: str) -> None:
    ctx = await boot(relational_backend, broker_bean(_adapter(cluster)), *beans)
    try:
        await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what=what)
    finally:
        await ctx.stop()


def _dead_letters(cluster: FakeKafkaCluster) -> list[tuple[bytes, str]]:
    return [(record.value, dict(record.headers)["x-dlt-reason"].decode()) for record in cluster.records(f"{TOPIC}.DLT")]


# -- one listener: the unit takes its settings -------------------------------------------------------------


@pytest.mark.backends(SQLITE_FILE)
async def test_a_serializable_listener_runs_in_a_serializable_unit(relational_backend: RelationalBackend) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    units: list[UnitOfWork | None] = []

    async def handle(repo: DeliveredRepository, message: Message) -> None:
        units.append(current_unit_of_work())
        await repo.save(Delivered(body="m"))

    await _run(relational_backend, cluster, _listener(handle, isolation=Isolation.SERIALIZABLE), what="committed")
    [unit] = units
    assert unit is not None and not unit.auto
    assert unit.isolation is Isolation.SERIALIZABLE
    assert unit.suspended is None  # the listener joined the delivery's unit: there is one, not two
    assert await committed_bodies(relational_backend) == ["m"]


@pytest.mark.backends(SQLITE_FILE)
async def test_a_listener_s_timeout_bounds_the_delivery_s_unit(relational_backend: RelationalBackend) -> None:
    """The first attempt outlives the listener's 0.2 s timeout: it rolls back and the delivery comes again."""
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"slow")
    attempts: list[int] = []
    deadlines: list[float | None] = []

    async def handle(repo: DeliveredRepository, message: Message) -> None:
        attempts.append(message.delivery_attempt)
        unit = current_unit_of_work()
        deadlines.append(unit.deadline if unit is not None else None)
        await repo.save(Delivered(body=f"attempt {message.delivery_attempt}"))
        if message.delivery_attempt == 1:
            await asyncio.sleep(1.0)

    await _run(relational_backend, cluster, _listener(handle, timeout=0.2), what="committed on the second attempt")
    assert attempts == [1, 2]
    assert all(deadline is not None for deadline in deadlines)
    assert await committed_bodies(relational_backend) == ["attempt 2"]
    assert cluster.records(f"{TOPIC}.DLT") == []


@pytest.mark.backends(SQLITE_FILE)
async def test_a_read_only_listener_cannot_write(relational_backend: RelationalBackend) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"write")
    read_only: list[bool] = []

    async def handle(repo: DeliveredRepository, message: Message) -> None:
        unit = current_unit_of_work()
        read_only.append(unit is not None and unit.read_only)
        await repo.save(Delivered(body="written by a read-only listener"))

    await _run(relational_backend, cluster, _listener(handle, read_only=True), what="dead-lettered")
    assert read_only == [True, True, True]
    assert await committed_bodies(relational_backend) == []
    assert _dead_letters(cluster) == [(b"write", "IllegalTransactionStateError")]


@pytest.mark.backends(SQLITE_FILE)
async def test_a_never_listener_runs_without_a_unit(relational_backend: RelationalBackend) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    units: list[UnitOfWork | None] = []

    async def handle(repo: DeliveredRepository, message: Message) -> None:
        units.append(current_unit_of_work())
        await repo.save(Delivered(body="m"))  # a short unit of its own

    await _run(relational_backend, cluster, _listener(handle, propagation=Propagation.NEVER), what="committed")
    assert units == [None]
    assert await committed_bodies(relational_backend) == ["m"]
    assert cluster.records(f"{TOPIC}.DLT") == []


@pytest.mark.backends(SQLITE_FILE)
async def test_a_requires_new_listener_runs_in_one_unit_of_its_own(relational_backend: RelationalBackend) -> None:
    """No unit of the container's around it: on SQLite a second one would wait for the lock the first holds."""
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    units: list[UnitOfWork | None] = []

    async def handle(repo: DeliveredRepository, message: Message) -> None:
        units.append(current_unit_of_work())
        await repo.save(Delivered(body="m"))

    await _run(relational_backend, cluster, _listener(handle, propagation=Propagation.REQUIRES_NEW), what="committed")
    [unit] = units
    assert unit is not None and unit.suspended is None
    assert await committed_bodies(relational_backend) == ["m"]
    assert cluster.records(f"{TOPIC}.DLT") == []


RETRIED: list[tuple[int, UnitOfWork | None]] = []


@service
class RetryingListener:
    """Retries its own transaction in process: each attempt must run in a fresh unit."""

    def __init__(self, repo: DeliveredRepository) -> None:
        self.repo = repo

    @message_listener(TOPIC, group=GROUP)
    @retry(max_attempts=2)
    @transactional
    async def on_message(self, message: Message) -> None:
        RETRIED.append((message.delivery_attempt, current_unit_of_work()))
        await self.repo.save(Delivered(body=f"try {len(RETRIED)}"))
        if len(RETRIED) == 1:
            raise RuntimeError("fails once")


@pytest.mark.backends(SQLITE_FILE)
async def test_a_listener_that_retries_in_process_gets_a_fresh_unit_per_attempt(
    relational_backend: RelationalBackend,
) -> None:
    """Inside a unit of the container's, the first failure left the unit rollback-only and @retry stopped:
    the in-process retry never ran and the broker delivered the record again."""
    RETRIED.clear()
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    await _run(relational_backend, cluster, RetryingListener, what="committed")
    assert [attempt for attempt, _unit in RETRIED] == [1, 1]  # both in the first delivery
    first, second = (unit for _attempt, unit in RETRIED)
    assert first is not None and second is not None and first is not second
    assert await committed_bodies(relational_backend) == ["try 2"]


# -- a caught failure of a joined @transactional -----------------------------------------------------------


@service
class Ledger:
    def __init__(self, repo: DeliveredRepository) -> None:
        self.repo = repo

    @transactional
    async def record(self, body: str) -> None:
        await self.repo.save(Delivered(body=body))

    @transactional
    async def audit(self, body: str) -> None:
        await self.repo.save(Delivered(body=body))
        raise RuntimeError("the audit trail is down")

    @transactional(propagation=Propagation.NESTED)
    async def audit_in_a_savepoint(self, body: str) -> None:
        await self.repo.save(Delivered(body=body))
        raise RuntimeError("the audit trail is down")


def _best_effort_listener(nested: bool) -> type:
    @service
    class BestEffortListener:
        def __init__(self, ledger: Ledger) -> None:
            self.ledger = ledger

        @message_listener(TOPIC, group=GROUP)
        async def on_message(self, message: Message) -> None:
            await self.ledger.record("kept")
            try:
                if nested:
                    await self.ledger.audit_in_a_savepoint("audit")
                else:
                    await self.ledger.audit("audit")
            except RuntimeError:
                pass  # best effort

    return BestEffortListener


@pytest.mark.backends(SQLITE_FILE)
async def test_a_caught_failure_of_a_joined_transactional_fails_the_delivery(
    relational_backend: RelationalBackend,
) -> None:
    """The failing participant marks the delivery's unit rollback-only: catching its exception does not
    undo that, the unit rolls back (UnexpectedRollbackError), and the delivery is dead-lettered."""
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    await _run(relational_backend, cluster, Ledger, _best_effort_listener(nested=False), what="dead-lettered")
    assert await committed_bodies(relational_backend) == []
    assert _dead_letters(cluster) == [(b"m", "UnexpectedRollbackError")]


@pytest.mark.backends(SQLITE_FILE)
async def test_a_best_effort_step_in_a_savepoint_leaves_the_delivery_to_commit(
    relational_backend: RelationalBackend,
) -> None:
    cluster = FakeKafkaCluster()
    cluster.append(TOPIC, b"m")
    await _run(relational_backend, cluster, Ledger, _best_effort_listener(nested=True), what="committed")
    assert await committed_bodies(relational_backend) == ["kept"]
    assert cluster.records(f"{TOPIC}.DLT") == []


# -- listeners that cannot share one unit ------------------------------------------------------------------

SHARED: dict[str, UnitOfWork | None] = {}


@service
class SerializableAndPlainListeners:
    def __init__(self, repo: DeliveredRepository) -> None:
        self.repo = repo

    @message_listener(TOPIC, group=GROUP)
    @transactional(isolation=Isolation.SERIALIZABLE)
    async def serializable(self, message: Message) -> None:
        SHARED["serializable"] = current_unit_of_work()
        await self.repo.save(Delivered(body="serializable"))

    @message_listener(TOPIC, group=GROUP)
    async def plain(self, message: Message) -> None:
        SHARED["plain"] = current_unit_of_work()
        await self.repo.save(Delivered(body="plain"))


@pytest.mark.backends(SQLITE_FILE)
async def test_listeners_that_cannot_share_a_unit_are_named_at_subscription_and_run_as_declared(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    SHARED.clear()
    cluster = FakeKafkaCluster()
    with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
        ctx = await boot(relational_backend, broker_bean(_adapter(cluster)), SerializableAndPlainListeners)
        try:
            await eventually(
                lambda: any("listener_units_differ" in record.getMessage() for record in caplog.records),
                what="the warning at subscription",
            )
            assert cluster.log_end(TOPIC) == 0  # before any record
            cluster.append(TOPIC, b"m")
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="committed")
        finally:
            await ctx.stop()
    [warning] = [record.getMessage() for record in caplog.records if "listener_units_differ" in record.getMessage()]
    assert "serializable (propagation=REQUIRED, isolation=SERIALIZABLE)" in warning
    assert "plain (no @transactional)" in warning
    serializable = SHARED["serializable"]
    assert serializable is not None and serializable.isolation is Isolation.SERIALIZABLE
    assert SHARED["plain"] is None  # no unit around it: its repository call got a short one
    assert await committed_bodies(relational_backend) == ["plain", "serializable"]


# -- the plan, listener by listener ------------------------------------------------------------------------


class Listeners:
    async def plain(self, message: Message) -> None: ...

    @transactional
    async def required(self, message: Message) -> None: ...

    @transactional(isolation=Isolation.SERIALIZABLE, timeout=5)
    async def serializable(self, message: Message) -> None: ...

    @transactional(isolation=Isolation.SERIALIZABLE, timeout=5, propagation=Propagation.MANDATORY)
    async def serializable_mandatory(self, message: Message) -> None: ...

    @transactional(isolation=Isolation.SERIALIZABLE, timeout=5, propagation=Propagation.NESTED)
    async def serializable_nested(self, message: Message) -> None: ...

    @transactional(propagation=Propagation.SUPPORTS)
    async def supports(self, message: Message) -> None: ...

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def requires_new(self, message: Message) -> None: ...

    @transactional(propagation=Propagation.NOT_SUPPORTED)
    async def not_supported(self, message: Message) -> None: ...

    @transactional(propagation=Propagation.NEVER)
    async def never(self, message: Message) -> None: ...

    @transactional(manager="reporting", read_only=True)
    async def reporting(self, message: Message) -> None: ...

    @retry(max_attempts=2)
    @transactional
    async def retried(self, message: Message) -> None: ...

    @retry(max_attempts=2)
    async def retried_plain(self, message: Message) -> None: ...


def test_the_settings_of_a_listener_are_found_through_its_endpoint() -> None:
    listeners = Listeners()
    endpoint = ListenerEndpoint(listeners.serializable, ListenerOptions())
    for wrapped in (listeners.serializable, endpoint, functools.partial(endpoint)):
        definition = transaction_definition_of(wrapped)
        assert definition is not None and definition.isolation is Isolation.SERIALIZABLE
    assert transaction_definition_of(listeners.plain) is None


def test_the_unit_takes_the_settings_of_the_listener_that_joins_it() -> None:
    listeners = Listeners()
    invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
    default = invoker.plan([listeners.plain])
    assert default == TransactionDefinition(name="listener t")
    assert invoker.plan([listeners.required]) == default
    unit = invoker.plan([ListenerEndpoint(listeners.serializable, ListenerOptions())])
    assert unit is not None
    assert (unit.propagation, unit.isolation, unit.timeout) == (Propagation.REQUIRED, Isolation.SERIALIZABLE, 5)
    mandatory = invoker.plan([listeners.serializable_mandatory])
    assert mandatory is not None and mandatory.isolation is Isolation.SERIALIZABLE
    reporting = invoker.plan([listeners.reporting])
    assert reporting is not None and (reporting.datasource, reporting.read_only) == ("reporting", True)


@pytest.mark.parametrize(
    "name", ["supports", "requires_new", "not_supported", "never", "serializable_nested", "retried", "retried_plain"]
)
def test_a_listener_that_needs_no_unit_of_the_container_s_gets_none(name: str) -> None:
    invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
    assert invoker.plan([getattr(Listeners(), name)]) is None


def test_listeners_with_the_same_settings_share_the_unit(caplog: pytest.LogCaptureFixture) -> None:
    listeners = Listeners()
    invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
    with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
        assert invoker.plan([listeners.required, listeners.plain, listeners.supports]) == invoker.plan(
            [listeners.plain]
        )
        shared = invoker.plan([listeners.serializable, listeners.serializable_mandatory, listeners.serializable_nested])
    assert shared is not None and shared.isolation is Isolation.SERIALIZABLE
    assert not caplog.records


@pytest.mark.parametrize(
    "names",
    [
        ("serializable", "plain"),
        ("plain", "requires_new"),
        ("required", "not_supported"),
        ("required", "never"),
        ("required", "reporting"),
        ("serializable", "supports"),
        ("plain", "retried"),
    ],
)
def test_listeners_that_cannot_share_a_unit_get_none_and_are_named_once(
    names: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    listeners = Listeners()
    chosen = [getattr(listeners, name) for name in names]
    invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
    with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
        assert invoker.plan(chosen) is None
        assert invoker.plan(chosen) is None
    [warning] = [record.getMessage() for record in caplog.records]
    assert "listener_units_differ container=t" in warning
    assert all(f"Listeners.{name} (" in warning for name in names)


def test_without_the_container_s_unit_there_is_no_plan_to_warn_about(caplog: pytest.LogCaptureFixture) -> None:
    listeners = Listeners()
    invoker = ListenerInvoker(ListenerContainerSettings(transactional=False), name="t")
    with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
        assert invoker.plan([listeners.serializable]) is None
        assert invoker.plan([listeners.serializable, listeners.plain]) is None
    assert not caplog.records


def test_a_listener_without_transactional_gets_the_configured_datasource() -> None:
    invoker = ListenerInvoker(ListenerContainerSettings(datasource="orders"), name="t")
    plain = invoker.plan([Listeners().plain])
    assert plain is not None and plain.datasource == "orders"
    required = invoker.plan([Listeners().required])  # its @transactional resolves the default datasource
    assert required is not None and required.datasource is None
