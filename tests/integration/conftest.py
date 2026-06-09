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
"""Shared fixtures for testcontainers-backed integration tests.

Each fixture starts a real backend in Docker (testcontainers) — or, if the matching
``PYFLY_IT_*`` env var is set (e.g. from ``docker compose up``), uses that URL instead and
starts no container. Failures normally ``skip`` so the suite degrades cleanly where Docker is
absent; setting ``PYFLY_INTEGRATION_REQUIRE_DOCKER=1`` (the CI integration job) flips skip -> FAIL
so missing backends cannot masquerade as "passing".
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest

from pyfly.testing import (
    is_docker_available,
    kafka_container,
    mongodb_container,
    mysql_container,
    postgres_container,
    pyfly_config_for,
    rabbitmq_container,
    redis_container,
)

_INTEGRATION_DIR = Path(__file__).resolve().parent
REQUIRE_DOCKER = os.environ.get("PYFLY_INTEGRATION_REQUIRE_DOCKER") == "1"


def unavailable(reason: str) -> NoReturn:
    """Skip the test — unless PYFLY_INTEGRATION_REQUIRE_DOCKER=1, then fail hard (CI gate)."""
    if REQUIRE_DOCKER:
        raise RuntimeError(reason)
    pytest.skip(reason)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-apply @pytest.mark.integration to everything collected under tests/integration/,
    then (if Docker is required but unreachable) abort the whole run so the CI job fails."""
    has_integration = False
    for item in items:
        item_path = Path(str(getattr(item, "path", item.fspath))).resolve()
        if _INTEGRATION_DIR == item_path or _INTEGRATION_DIR in item_path.parents:
            item.add_marker(pytest.mark.integration)
            if "test_foundation_wiring.py" not in item_path.name and "test_marker" not in item_path.name:
                has_integration = True
    if REQUIRE_DOCKER and has_integration and not is_docker_available():
        raise pytest.UsageError("PYFLY_INTEGRATION_REQUIRE_DOCKER=1 but no Docker daemon is reachable")


def _started(factory, exposed: str):  # type: ignore[no-untyped-def]
    """Start a container via *factory*, or skip/fail via unavailable() on any startup error."""
    try:
        container = factory()
        container.start()
    except Exception as exc:  # noqa: BLE001 — daemon present but cannot run/pull -> skip (or fail in CI)
        unavailable(f"{exposed} testcontainer unavailable: {exc}")
    return container


@pytest.fixture
def redis_url() -> Iterator[str]:
    env = os.environ.get("PYFLY_IT_REDIS_URL")
    if env:
        yield env
        return
    container = _started(redis_container, "Redis")
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture
def pg_url() -> Iterator[str]:
    env = os.environ.get("PYFLY_IT_POSTGRES_URL")
    if env:
        yield env
        return
    container = _started(postgres_container, "Postgres")
    try:
        yield pyfly_config_for(container)["pyfly.data.relational.url"]
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture(scope="session")
def mysql_url() -> Iterator[str]:
    env = os.environ.get("PYFLY_IT_MYSQL_URL")
    if env:
        yield env
        return
    container = _started(mysql_container, "MySQL")
    try:
        yield pyfly_config_for(container)["pyfly.data.relational.url"]
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture(scope="session")
def mongo_url() -> Iterator[str]:
    env = os.environ.get("PYFLY_IT_MONGO_URI")
    if env:
        yield env
        return
    container = _started(mongodb_container, "MongoDB")
    try:
        yield pyfly_config_for(container)["pyfly.data.document.uri"]
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture(scope="session")
def kafka_url() -> Iterator[str]:
    env = os.environ.get("PYFLY_IT_KAFKA_BOOTSTRAP")
    if env:
        yield env
        return
    container = _started(kafka_container, "Kafka")
    try:
        yield pyfly_config_for(container)["pyfly.eda.kafka.bootstrap-servers"]
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture(scope="session")
def amqp_url() -> Iterator[str]:
    env = os.environ.get("PYFLY_IT_AMQP_URL")
    if env:
        yield env
        return
    container = _started(rabbitmq_container, "RabbitMQ")
    try:
        yield pyfly_config_for(container)["pyfly.eda.rabbitmq.url"]
    finally:
        with contextlib.suppress(Exception):
            container.stop()
