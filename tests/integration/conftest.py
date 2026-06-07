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

``@requires_docker`` only checks that a Docker *daemon* answers — but CI can have a daemon that
cannot *pull* images (registry timeout). These fixtures therefore start the container inside a
guard and ``pytest.skip`` on any startup failure, so the suite degrades to "skipped" (never
"errored") wherever Docker isn't fully functional.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from pyfly.testing import postgres_container, pyfly_config_for, redis_container


@pytest.fixture
def redis_url() -> Iterator[str]:
    try:
        container = redis_container()
        container.start()
    except Exception as exc:  # noqa: BLE001 — daemon present but cannot run/pull -> skip, don't error
        pytest.skip(f"Redis testcontainer unavailable: {exc}")
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture
def pg_url() -> Iterator[str]:
    try:
        container = postgres_container()
        container.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres testcontainer unavailable: {exc}")
    try:
        yield pyfly_config_for(container)["pyfly.data.relational.url"]
    finally:
        with contextlib.suppress(Exception):
            container.stop()
