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
"""Unit test: pyfly_config_for maps a RabbitMQ container to pyfly config keys."""
from __future__ import annotations

from pyfly.testing import pyfly_config_for


class _FakeRabbitMq:  # mimics testcontainers RabbitMqContainer surface we use
    def __init__(self) -> None:
        self.__class__.__name__ = "RabbitMqContainer"

    def get_container_host_ip(self) -> str:
        return "127.0.0.1"

    def get_exposed_port(self, port: int) -> int:
        assert port == 5672
        return 55672


def test_pyfly_config_for_maps_rabbitmq() -> None:
    cfg = pyfly_config_for(_FakeRabbitMq())
    assert cfg["pyfly.eda.rabbitmq.url"] == "amqp://guest:guest@127.0.0.1:55672/"
    assert cfg["pyfly.messaging.rabbitmq.url"] == "amqp://guest:guest@127.0.0.1:55672/"
