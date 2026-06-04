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
"""Regression tests for config placeholder env resolution (#87, #89)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config, config_properties


def test_placeholder_honors_relaxed_env_override(monkeypatch) -> None:
    # ${app.name} must resolve via the PYFLY_APP_NAME relaxed env mapping, and
    # the env override must win over the raw file value (audit #87/#89).
    monkeypatch.setenv("PYFLY_APP_NAME", "from-env")
    cfg = Config({"app": {"name": "from-file"}, "greeting": "Hi ${app.name}"})
    assert cfg.get("greeting") == "Hi from-env"


def test_placeholder_falls_back_to_file_when_no_env() -> None:
    cfg = Config({"app": {"name": "from-file"}, "greeting": "Hi ${app.name}"})
    assert cfg.get("greeting") == "Hi from-file"


# --- #118: @config_properties classes are injectable beans, bound from config ---


@config_properties(prefix="db")
@dataclass
class DbProps:
    url: str = "default"
    pool_size: int = 5


@service
class UsesDb:
    def __init__(self, props: DbProps) -> None:
        self.props = props


@pytest.mark.asyncio
async def test_config_properties_is_injectable_and_bound() -> None:
    ctx = ApplicationContext(Config({"db": {"url": "postgres://x", "pool-size": 20}}))
    ctx.register_bean(DbProps)
    ctx.register_bean(UsesDb)
    await ctx.start()
    try:
        uses = ctx.get_bean(UsesDb)
        assert uses.props.url == "postgres://x"  # bound from config (audit #118)
        assert uses.props.pool_size == 20  # relaxed kebab → snake binding
    finally:
        await ctx.stop()
