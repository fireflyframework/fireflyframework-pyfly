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
"""Regression tests for cqrs audit fixes (#94, #100)."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs.config.auto_configuration import CqrsAutoConfiguration
from pyfly.cqrs.types import Query


def test_metrics_service_receives_registry() -> None:
    # audit #94 — the bean must wire the injected MetricsRegistry through.
    class _Registry:
        def counter(self, *a: object, **k: object) -> object:
            return object()

        def histogram(self, *a: object, **k: object) -> object:
            return object()

        def gauge(self, *a: object, **k: object) -> object:
            return object()

    service = CqrsAutoConfiguration().cqrs_metrics_service(registry=_Registry())
    assert service._registry is not None  # noqa: SLF001


def test_cache_key_is_deterministic_and_not_builtin_hash() -> None:
    @dataclass
    class FindUser(Query):
        user_id: str = ""

    q1 = FindUser(user_id="abc")
    q2 = FindUser(user_id="abc")
    q3 = FindUser(user_id="xyz")

    # Same fields → same key across instances (and would across processes,
    # unlike the process-randomized builtin hash() — audit #100).
    assert q1.get_cache_key() == q2.get_cache_key()
    assert q1.get_cache_key() != q3.get_cache_key()
    assert q1.get_cache_key().startswith("FindUser:")
