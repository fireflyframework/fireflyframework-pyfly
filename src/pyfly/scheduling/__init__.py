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
"""PyFly Scheduling — periodic task execution with cron, fixed-rate, and fixed-delay modes.

Import concrete adapter types from the adapter package::

    from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
    from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor
"""

from pyfly.scheduling.decorators import async_method, scheduled
from pyfly.scheduling.lock import DistributedLock, InProcessDistributedLock, LocalLock
from pyfly.scheduling.ports.outbound import TaskExecutorPort

__all__ = [
    "DistributedLock",
    "InProcessDistributedLock",
    "LocalLock",
    "TaskExecutorPort",
    "async_method",
    "scheduled",
]

try:
    from pyfly.scheduling.cron import CronExpression
    from pyfly.scheduling.task_scheduler import TaskScheduler

    __all__ += ["CronExpression", "TaskScheduler"]
except ImportError:
    pass
