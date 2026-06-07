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
"""PyFly Testing — Test utilities, assertions, and fixtures."""

from pyfly.testing.assertions import assert_event_published, assert_no_events_published
from pyfly.testing.client import PyFlyTestClient, TestResponse
from pyfly.testing.containers import create_test_container
from pyfly.testing.fixtures import PyFlyTestCase
from pyfly.testing.mock import mock_bean
from pyfly.testing.slices import DataTest, ServiceTest, WebTest, get_test_slice
from pyfly.testing.testcontainers import (
    is_docker_available,
    kafka_container,
    mongodb_container,
    mysql_container,
    postgres_container,
    pyfly_config,
    pyfly_config_for,
    redis_container,
    requires_docker,
)

__all__ = [
    "DataTest",
    "PyFlyTestCase",
    "PyFlyTestClient",
    "ServiceTest",
    "TestResponse",
    "WebTest",
    "assert_event_published",
    "assert_no_events_published",
    "create_test_container",
    "get_test_slice",
    "is_docker_available",
    "kafka_container",
    "mock_bean",
    "mongodb_container",
    "mysql_container",
    "postgres_container",
    "pyfly_config",
    "pyfly_config_for",
    "redis_container",
    "requires_docker",
]
