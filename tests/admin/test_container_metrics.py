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
"""Tests for container bean metrics."""

import time

from pyfly.container.container import Container
from pyfly.container.metrics import BeanMetrics


class TestBeanMetrics:
    def test_default_values(self):
        m = BeanMetrics()
        assert m.creation_time_ns == 0
        assert m.resolution_count == 0
        assert m.created_at is None

    def test_increment_resolution(self):
        m = BeanMetrics()
        m.resolution_count += 1
        assert m.resolution_count == 1

    def test_set_creation_time(self):
        m = BeanMetrics()
        m.creation_time_ns = 1_500_000
        assert m.creation_time_ns == 1_500_000

    def test_set_created_at(self):
        now = time.time()
        m = BeanMetrics(created_at=now)
        assert m.created_at == now


class Greeter:
    def greet(self) -> str:
        return "hello"


class UserService:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


class TestContainerMetrics:
    def test_tracks_creation_time(self):
        c = Container()
        c.register(Greeter)
        c.resolve(Greeter)
        metrics = c.get_bean_metrics(Greeter)
        assert metrics is not None
        assert metrics.creation_time_ns > 0
        assert metrics.created_at is not None

    def test_tracks_resolution_count(self):
        c = Container()
        c.register(Greeter)
        c.resolve(Greeter)
        c.resolve(Greeter)
        c.resolve(Greeter)
        metrics = c.get_bean_metrics(Greeter)
        assert metrics is not None
        assert metrics.resolution_count == 3

    def test_all_metrics_returns_dict(self):
        c = Container()
        c.register(Greeter)
        c.resolve(Greeter)
        all_metrics = c.get_all_metrics()
        assert Greeter in all_metrics

    def test_dependency_resolution_counted(self):
        c = Container()
        c.register(Greeter)
        c.register(UserService)
        c.resolve(UserService)
        greeter_metrics = c.get_bean_metrics(Greeter)
        assert greeter_metrics is not None
        assert greeter_metrics.resolution_count >= 1

    def test_metrics_none_for_unresolved(self):
        c = Container()
        c.register(Greeter)
        metrics = c.get_bean_metrics(Greeter)
        assert metrics is None
