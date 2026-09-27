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
"""The broker-neutral parts of the listener container: the retry policy and its back-offs, the
classification of transient failures (on real SQLite and pool errors), the settings read from
configuration, and the invoker that runs a delivery in the container's unit of work."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.core.config import Config
from pyfly.data.transaction import (
    CommitOutcomeUnknownError,
    IllegalTransactionStateError,
    TransactionTimedOutError,
    current_unit_of_work,
    is_transaction_active,
)
from pyfly.messaging.listener_container import (
    ATTEMPT_HEADER,
    ConcurrencyLimit,
    DeliveryState,
    ExponentialBackOff,
    FixedBackOff,
    LinearBackOff,
    ListenerContainerSettings,
    ListenerInvoker,
    ListenerOptions,
    PoisonMessageError,
    RetryPolicy,
    dead_letter_headers,
    delivery_attempt,
    is_transient_failure,
)
from tests.messaging.listener_app import Delivered, DeliveredRepository, boot
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend

# -- back-off and retry policy ---------------------------------------------------------------------------


def test_the_back_offs() -> None:
    assert [FixedBackOff(0.3).delay_after(n) for n in (1, 2, 3)] == [0.3, 0.3, 0.3]
    assert [LinearBackOff(0.5).delay_after(n) for n in (1, 2, 3)] == [0.5, 1.0, 1.5]
    assert [ExponentialBackOff().delay_after(n) for n in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_the_default_policy_retries_with_a_delay_that_is_not_zero() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == 5
    assert policy.retry_delay(RuntimeError(), 1) == 1.0
    assert policy.retry_delay(RuntimeError(), 4) == 8.0
    assert policy.retry_delay(RuntimeError(), 5) is None  # the fifth attempt was the last


def test_a_message_that_cannot_be_read_is_never_retried() -> None:
    assert RetryPolicy().retry_delay(PoisonMessageError(ValueError("not json")), 1) is None


def test_not_retryable_types_skip_the_remaining_attempts_unless_the_failure_is_transient() -> None:
    policy = RetryPolicy(not_retryable=(ValueError, ConnectionError))
    assert policy.retry_delay(ValueError("invalid order"), 1) is None
    assert policy.retry_delay(ConnectionResetError("db restarting"), 1) == 1.0
    assert policy.retry_delay(KeyError("x"), 1) == 1.0


def test_a_transient_failure_uses_up_the_same_attempts() -> None:
    """Every failure counts: a lost connection on the last attempt is dead-lettered like any other, so a
    database outage longer than the back-off dead-letters what is consumed meanwhile."""
    policy = RetryPolicy(max_attempts=3)
    assert policy.retry_delay(ConnectionResetError("db restarting"), 2) == 2.0
    assert policy.retry_delay(ConnectionResetError("db restarting"), 3) is None


def test_a_listener_s_options_override_the_policy() -> None:
    policy = RetryPolicy(max_attempts=5, backoff=ExponentialBackOff())
    assert policy.with_options(None) is policy
    overridden = policy.with_options(ListenerOptions(max_attempts=1, backoff=LinearBackOff(0.1)))
    assert (overridden.max_attempts, overridden.backoff) == (1, LinearBackOff(0.1))
    assert policy.with_options(ListenerOptions(dead_letter="x")) == policy


def test_a_policy_needs_one_attempt_at_least() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


# -- transient failures, on real errors ------------------------------------------------------------------


async def _locked_error(tmp_path: Path) -> OperationalError:
    """A real "database is locked": another connection holds the SQLite write lock."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'locked.db'}"
    holder = create_async_engine(url, connect_args={"timeout": 0})
    writer = create_async_engine(url, connect_args={"timeout": 0})
    try:
        async with holder.begin() as conn:
            await conn.execute(text("CREATE TABLE t (x INTEGER)"))
        async with holder.begin() as locked:
            await locked.execute(text("INSERT INTO t VALUES (1)"))
            with pytest.raises(OperationalError) as caught:
                async with writer.begin() as conn:
                    await conn.execute(text("INSERT INTO t VALUES (2)"))
        return caught.value
    finally:
        await holder.dispose()
        await writer.dispose()


async def test_a_locked_sqlite_database_is_transient(tmp_path: Path) -> None:
    error = await _locked_error(tmp_path)
    assert "database is locked" in str(error)
    assert is_transient_failure(error)
    try:
        raise RuntimeError("the listener gave up") from error
    except RuntimeError as wrapped:
        assert is_transient_failure(wrapped)  # found through __cause__


async def test_a_pool_timeout_is_transient(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}", pool_size=1, max_overflow=0, pool_timeout=0.05
    )
    try:
        async with engine.connect():
            with pytest.raises(PoolTimeoutError) as caught:
                async with engine.connect():
                    pass
    finally:
        await engine.dispose()
    assert is_transient_failure(caught.value)


async def test_a_constraint_violation_is_not_transient(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unique.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (x INTEGER PRIMARY KEY)"))
            await conn.execute(text("INSERT INTO t VALUES (1)"))
        with pytest.raises(IntegrityError) as caught:
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO t VALUES (1)"))
    finally:
        await engine.dispose()
    assert not is_transient_failure(caught.value)
    assert not is_transient_failure(ValueError("invalid"))


def test_timeouts_and_lost_connections_are_transient() -> None:
    assert is_transient_failure(TransactionTimedOutError("unit exceeded its timeout"))
    assert is_transient_failure(CommitOutcomeUnknownError("commit interrupted"))
    assert is_transient_failure(TimeoutError())
    assert is_transient_failure(ConnectionResetError())


# -- settings --------------------------------------------------------------------------------------------


def test_settings_default_to_a_bounded_prefetch_and_a_ten_second_drain() -> None:
    settings = ListenerContainerSettings.from_config(Config({}), "pyfly.messaging")
    assert settings == ListenerContainerSettings()
    assert (settings.prefetch, settings.shutdown_timeout, settings.transactional) == (20, 10.0, True)
    assert settings.concurrency is None and settings.datasource is None


def test_settings_are_read_under_their_prefix() -> None:
    config = Config(
        {
            "pyfly": {
                "eda": {
                    "rabbitmq": {"prefetch": "5"},
                    "listener": {"concurrency": "2", "retry": {"max-attempts": "2", "initial-delay": "0.25"}},
                }
            }
        }
    )
    settings = ListenerContainerSettings.from_config(config, "pyfly.eda")
    assert (settings.prefetch, settings.concurrency, settings.retry.max_attempts) == (5, 2, 2)
    assert settings.retry.backoff == ExponentialBackOff(initial=0.25)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"prefetch": 0}, "prefetch"),
        ({"concurrency": 0}, "concurrency"),
        ({"shutdown_timeout": -1.0}, "shutdown_timeout"),
        ({"max_poll_records": 0}, "max_poll_records"),
    ],
)
def test_invalid_settings_are_refused(values: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ListenerContainerSettings(**values)  # type: ignore[arg-type]


# -- delivery bookkeeping --------------------------------------------------------------------------------


class _Delivery:
    def __init__(self, headers: dict[str, object] | None) -> None:
        self.headers = headers


def test_the_attempt_of_an_amqp_delivery_comes_from_its_header() -> None:
    assert delivery_attempt(_Delivery(None)) == 1
    assert delivery_attempt(_Delivery({ATTEMPT_HEADER: 3})) == 3
    assert delivery_attempt(_Delivery({ATTEMPT_HEADER: b"2"})) == 2
    assert delivery_attempt(_Delivery({ATTEMPT_HEADER: "garbage"})) == 1


def test_dead_letter_headers_name_the_cause_of_a_poison_message() -> None:
    headers = dead_letter_headers(
        topic="orders", error=PoisonMessageError(ValueError("bad json")), attempts=1, partition=0, offset=7
    )
    assert headers["x-exception"] == headers["x-dlt-reason"] == "ValueError"
    assert headers["x-exception-message"] == "bad json"
    assert (headers["x-dlt-source-partition"], headers["x-dlt-source-offset"]) == ("0", "7")
    assert headers["x-original-topic"] == headers["x-dlt-source-topic"] == "orders"


async def test_the_concurrency_limit_is_sized_at_the_first_delivery() -> None:
    sizes = iter([2])
    limit = ConcurrencyLimit(lambda: next(sizes))
    assert limit.size is None
    running = 0
    peak = 0

    async def work() -> None:
        nonlocal running, peak
        async with limit:
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert (limit.size, peak) == (2, 2)


# -- the container's unit of work ------------------------------------------------------------------------


async def test_without_a_data_layer_deliveries_run_without_a_unit() -> None:
    invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
    assert invoker.manager() is None
    seen: list[bool] = []

    async def call() -> None:
        seen.append(is_transaction_active())

    state = DeliveryState()
    await invoker.invoke(call, state)
    assert seen == [False] and state.committed
    assert invoker.suggested_concurrency(20) == 20


async def test_a_datasource_the_settings_name_must_exist() -> None:
    invoker = ListenerInvoker(ListenerContainerSettings(datasource="reporting"), name="t")
    with pytest.raises(IllegalTransactionStateError):
        invoker.manager()


@pytest.mark.backends(SQLITE_FILE)
async def test_a_delivery_runs_in_a_unit_that_commits_before_invoke_returns(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await boot(relational_backend)
    try:
        repo = ctx.get_bean(DeliveredRepository)
        invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
        assert invoker.suggested_concurrency(20) == 1  # SQLite has one writer
        units: list[object] = []

        async def call() -> None:
            units.append(current_unit_of_work())
            await repo.save(Delivered(body="kept"))

        state = DeliveryState()
        await invoker.invoke(call, state)
        assert state.committed and units[0] is not None

        async def failing() -> None:
            await repo.save(Delivered(body="rolled back"))
            raise RuntimeError("boom")

        failed = DeliveryState()
        with pytest.raises(RuntimeError):
            await invoker.invoke(failing, failed)
        assert not failed.committed
        assert [row.body for row in await repo.find_all()] == ["kept"]
    finally:
        await ctx.stop()


@pytest.mark.backends(SQLITE_FILE)
async def test_a_unit_that_committed_before_a_cancellation_still_counts_as_committed(
    relational_backend: RelationalBackend,
) -> None:
    """The cancellation lands while the commit runs (shielded): the delivery may be acknowledged."""
    ctx = await boot(relational_backend)
    try:
        repo = ctx.get_bean(DeliveredRepository)
        invoker = ListenerInvoker(ListenerContainerSettings(), name="t")
        state = DeliveryState()
        body_done = asyncio.Event()

        async def call() -> None:
            await repo.save(Delivered(body="committed"))
            body_done.set()

        task = asyncio.create_task(invoker.invoke(call, state))
        await body_done.wait()
        task.cancel()  # the body has returned: the boundary is committing
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.committed
        assert [row.body for row in await repo.find_all()] == ["committed"]
    finally:
        await ctx.stop()


@pytest.mark.parametrize(
    ("messaging", "key"),
    [
        ({"listener": {"retry": {"max-attempts": "0"}}}, "pyfly.messaging.listener.retry.max-attempts"),
        ({"listener": {"retry": {"max-attempts": "many"}}}, "pyfly.messaging.listener.retry.max-attempts"),
        ({"listener": {"retry": {"initial-delay": "-1"}}}, "pyfly.messaging.listener.retry.initial-delay"),
        ({"listener": {"shutdown-timeout": "soon"}}, "pyfly.messaging.listener.shutdown-timeout"),
        ({"listener": {"shutdown-timeout": "-1"}}, "pyfly.messaging.listener.shutdown-timeout"),
        ({"listener": {"transactional": "maybe"}}, "pyfly.messaging.listener.transactional"),
        ({"listener": {"concurrency": "0"}}, "pyfly.messaging.listener.concurrency"),
        ({"rabbitmq": {"prefetch": "0"}}, "pyfly.messaging.rabbitmq.prefetch"),
        ({"kafka": {"max-poll-records": "0"}}, "pyfly.messaging.kafka.max-poll-records"),
    ],
)
def test_a_configured_value_that_is_invalid_is_refused_naming_its_key(messaging: dict[str, object], key: str) -> None:
    config = Config({"pyfly": {"messaging": messaging}})
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        ListenerContainerSettings.from_config(config, "pyfly.messaging")


def test_the_kafka_poll_size_is_read_from_configuration() -> None:
    config = Config({"pyfly": {"eda": {"kafka": {"max-poll-records": "10"}}}})
    assert ListenerContainerSettings.from_config(config, "pyfly.eda").max_poll_records == 10
