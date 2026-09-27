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
"""The phase of a lifecycle bean (``SmartLifecycle``) and the resource-registry protocol."""

from __future__ import annotations

from typing import Any

from pyfly.kernel import CONSUMER_PHASE, DEFAULT_PHASE, ResourceRegistry, SmartLifecycle, lifecycle_phase
from pyfly.kernel.lifecycle import is_resource_registry


class _Plain:
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _Declared(_Plain):
    phase = -5


class _PropertyPhase(_Plain):
    @property
    def phase(self) -> int:
        return 42


class _Bus(_Plain):
    def subscribe(self, pattern: str, handler: Any) -> None: ...


class _BusInSchemaPhase(_Bus):
    phase = -100


class _TextPhase(_Plain):
    phase = "draining"


class _Registry:
    async def dispose_all(self) -> None: ...


class _SyncDisposer:
    def dispose_all(self) -> None: ...


def test_a_bean_without_a_phase_is_in_the_default_phase() -> None:
    assert lifecycle_phase(_Plain()) == DEFAULT_PHASE


def test_a_declared_phase_wins() -> None:
    assert lifecycle_phase(_Declared()) == -5
    assert lifecycle_phase(_PropertyPhase()) == 42
    assert lifecycle_phase(_BusInSchemaPhase()) == -100
    assert isinstance(_Declared(), SmartLifecycle)


def test_a_bean_that_takes_subscriptions_is_a_consumer() -> None:
    assert lifecycle_phase(_Bus()) == CONSUMER_PHASE


def test_a_phase_that_is_not_an_int_is_ignored() -> None:
    assert lifecycle_phase(_TextPhase()) == DEFAULT_PHASE


def test_a_resource_registry_disposes_with_a_coroutine() -> None:
    assert is_resource_registry(_Registry())
    assert isinstance(_Registry(), ResourceRegistry)
    assert not is_resource_registry(_SyncDisposer())
    assert not is_resource_registry(_Plain())
