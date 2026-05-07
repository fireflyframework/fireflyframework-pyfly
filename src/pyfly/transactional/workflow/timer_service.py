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
"""Workflow timer service — sleeps for a delay, then resumes the workflow."""

from __future__ import annotations

import asyncio


class TimerService:
    """Simple in-process timer based on :func:`asyncio.sleep`."""

    async def sleep_ms(self, delay_ms: int) -> None:
        if delay_ms <= 0:
            return
        await asyncio.sleep(delay_ms / 1000.0)
