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
"""Discovery must not evaluate bean descriptors while finding scheduled methods."""

from __future__ import annotations

import asyncio
import warnings
from datetime import timedelta
from functools import cached_property
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic.warnings import PydanticDeprecatedSince211

from pyfly.scheduling.decorators import scheduled
from pyfly.scheduling.task_scheduler import TaskScheduler


@pytest.mark.asyncio
async def test_pydantic_settings_do_not_warn_and_scheduled_bindings_still_execute() -> None:
    observed = {name: asyncio.Event() for name in ("model", "inherited", "static", "class")}

    class Settings(BaseModel):
        name: str = "settings"

        @scheduled(fixed_delay=timedelta(milliseconds=10))
        async def model_job(self) -> None:
            assert self.name == "settings"
            observed["model"].set()

    class ParentJobs:
        @scheduled(fixed_delay=timedelta(milliseconds=10))
        async def inherited_job(self) -> None:
            observed["inherited"].set()

    class Jobs(ParentJobs):
        @staticmethod
        @scheduled(fixed_delay=timedelta(milliseconds=10))
        async def static_job() -> None:
            observed["static"].set()

        @classmethod
        @scheduled(fixed_delay=timedelta(milliseconds=10))
        async def class_job(cls) -> None:
            assert cls is Jobs
            observed["class"].set()

    scheduler = TaskScheduler()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", PydanticDeprecatedSince211)
        count = scheduler.discover([Settings(), Jobs()])
    assert not caught, [str(warning.message) for warning in caught]
    assert count == 4
    try:
        await scheduler.start()
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in observed.values())), timeout=1)
    finally:
        await scheduler.stop()


def test_discovery_does_not_evaluate_custom_descriptors_or_properties() -> None:
    accessed: list[str] = []

    class Descriptor:
        def __get__(self, instance: Any, owner: type | None = None) -> Any:
            accessed.append("descriptor")
            raise RuntimeError("must not execute during discovery")

    class Bean:
        metadata = Descriptor()

        @property
        def value(self) -> str:
            accessed.append("property")
            raise RuntimeError("must not execute during discovery")

        @cached_property
        def cached(self) -> str:
            accessed.append("cached_property")
            raise RuntimeError("must not execute during discovery")

        @scheduled(fixed_delay=timedelta(seconds=1))
        async def job(self) -> None:
            pass

    assert TaskScheduler().discover([Bean()]) == 1
    assert accessed == []
