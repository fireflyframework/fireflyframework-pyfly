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
"""PyFly Testing — Test utilities, assertions, and fixtures.

The public names load on first use (PEP 562): pytest loads PyFly's plugin (``pyfly.testing.pytest_plugin``) in
every run where PyFly is installed, and importing them all with the package (the web client, the application
context, the container helpers) cost every run about 0.16 s.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.testing.assertions import assert_event_published, assert_no_events_published
    from pyfly.testing.client import PyFlyTestClient, TestResponse
    from pyfly.testing.containers import create_test_container
    from pyfly.testing.feature_flags import FlagOverrides, override_flags
    from pyfly.testing.fixtures import PyFlyTestCase
    from pyfly.testing.mock import mock_bean
    from pyfly.testing.rollback import RollbackTransaction
    from pyfly.testing.slice_context import data_slice, service_slice, slice_context, web_slice
    from pyfly.testing.slices import DataTest, ServiceTest, WebTest, get_test_slice
    from pyfly.testing.statement_counter import RecordedStatement, StatementCounter
    from pyfly.testing.testcontainers import (
        MongoDbReplicaSetContainer,
        is_docker_available,
        kafka_container,
        mariadb_container,
        mongodb_container,
        mongodb_replica_set_container,
        mysql_container,
        postgres_container,
        pyfly_config,
        pyfly_config_for,
        rabbitmq_container,
        redis_container,
        requires_docker,
    )

# The module of each public name.
_EXPORTS: dict[str, str] = {
    "assert_event_published": "pyfly.testing.assertions",
    "assert_no_events_published": "pyfly.testing.assertions",
    "PyFlyTestClient": "pyfly.testing.client",
    "TestResponse": "pyfly.testing.client",
    "create_test_container": "pyfly.testing.containers",
    "FlagOverrides": "pyfly.testing.feature_flags",
    "override_flags": "pyfly.testing.feature_flags",
    "PyFlyTestCase": "pyfly.testing.fixtures",
    "mock_bean": "pyfly.testing.mock",
    "RollbackTransaction": "pyfly.testing.rollback",
    "data_slice": "pyfly.testing.slice_context",
    "service_slice": "pyfly.testing.slice_context",
    "slice_context": "pyfly.testing.slice_context",
    "web_slice": "pyfly.testing.slice_context",
    "DataTest": "pyfly.testing.slices",
    "ServiceTest": "pyfly.testing.slices",
    "WebTest": "pyfly.testing.slices",
    "get_test_slice": "pyfly.testing.slices",
    "RecordedStatement": "pyfly.testing.statement_counter",
    "StatementCounter": "pyfly.testing.statement_counter",
    "MongoDbReplicaSetContainer": "pyfly.testing.testcontainers",
    "is_docker_available": "pyfly.testing.testcontainers",
    "kafka_container": "pyfly.testing.testcontainers",
    "mariadb_container": "pyfly.testing.testcontainers",
    "mongodb_container": "pyfly.testing.testcontainers",
    "mongodb_replica_set_container": "pyfly.testing.testcontainers",
    "mysql_container": "pyfly.testing.testcontainers",
    "postgres_container": "pyfly.testing.testcontainers",
    "pyfly_config": "pyfly.testing.testcontainers",
    "pyfly_config_for": "pyfly.testing.testcontainers",
    "rabbitmq_container": "pyfly.testing.testcontainers",
    "redis_container": "pyfly.testing.testcontainers",
    "requires_docker": "pyfly.testing.testcontainers",
}

__all__ = [
    "DataTest",
    "FlagOverrides",
    "MongoDbReplicaSetContainer",
    "PyFlyTestCase",
    "PyFlyTestClient",
    "RecordedStatement",
    "RollbackTransaction",
    "ServiceTest",
    "StatementCounter",
    "TestResponse",
    "WebTest",
    "assert_event_published",
    "assert_no_events_published",
    "create_test_container",
    "data_slice",
    "get_test_slice",
    "is_docker_available",
    "kafka_container",
    "mariadb_container",
    "mock_bean",
    "mongodb_container",
    "mongodb_replica_set_container",
    "mysql_container",
    "override_flags",
    "postgres_container",
    "pyfly_config",
    "pyfly_config_for",
    "rabbitmq_container",
    "redis_container",
    "requires_docker",
    "service_slice",
    "slice_context",
    "web_slice",
]


def __getattr__(name: str) -> Any:
    """Import the public name *name* from its module on first use (and a submodule of the package, as the
    package's attribute, the way importing everything up front made each one)."""
    module = _EXPORTS.get(name)
    if module is not None:
        value = getattr(importlib.import_module(module), name)
        globals()[name] = value
        return value
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"{__name__}.{name}":
            raise
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
