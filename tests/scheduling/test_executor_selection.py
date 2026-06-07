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
"""TaskScheduler executor backend selection (v26.06.67)."""

from __future__ import annotations

from typing import Any

from pyfly.container.container import Container
from pyfly.core.config import Config
from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor
from pyfly.scheduling.auto_configuration import SchedulingAutoConfiguration


def _executor(config: Config) -> Any:
    return SchedulingAutoConfiguration().task_scheduler(Container(), config)._executor


def test_default_executor_is_asyncio() -> None:
    assert isinstance(_executor(Config({})), AsyncIOTaskExecutor)


def test_thread_executor_selected_by_config() -> None:
    cfg = Config({"pyfly": {"scheduling": {"executor": {"type": "thread"}}}})
    assert isinstance(_executor(cfg), ThreadPoolTaskExecutor)


def test_thread_executor_respects_max_workers() -> None:
    cfg = Config({"pyfly": {"scheduling": {"executor": {"type": "thread", "max-workers": 7}}}})
    executor = _executor(cfg)
    assert isinstance(executor, ThreadPoolTaskExecutor)
    assert executor._executor._max_workers == 7
