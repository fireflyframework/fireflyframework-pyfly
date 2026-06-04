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
"""The configured saga compensation_policy reaches the engine (audit #170)."""

from __future__ import annotations

import pytest

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.transactional.core.model import CompensationPolicy
from pyfly.transactional.saga.engine.saga_engine import SagaEngine


@pytest.mark.asyncio
async def test_configured_compensation_policy_reaches_engine() -> None:
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "transactional": {
                        "enabled": "true",
                        "saga": {"compensation_policy": "GROUPED_PARALLEL"},
                    }
                }
            }
        )
    )
    await ctx.start()
    try:
        engine = ctx.get_bean(SagaEngine)
        assert engine._default_compensation_policy == CompensationPolicy.GROUPED_PARALLEL  # audit #170
    finally:
        await ctx.stop()
