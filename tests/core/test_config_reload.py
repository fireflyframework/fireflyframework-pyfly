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
"""Config runtime reload (v26.06.58) — Config.reload_from_sources + RefreshScope integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyfly.container.refresh_scope import refresh_scope
from pyfly.context.application_context import ApplicationContext
from pyfly.context.refresh import ContextRefresher
from pyfly.core.config import Config


def test_reload_picks_up_file_changes(tmp_path: Path) -> None:
    cfg_file = tmp_path / "pyfly.yaml"
    cfg_file.write_text("app:\n  name: alpha\n")
    cfg = Config.from_sources(tmp_path, load_defaults=False)
    assert cfg.get("app.name") == "alpha"

    cfg_file.write_text("app:\n  name: beta\n")
    assert cfg.reload_from_sources() is True
    assert cfg.get("app.name") == "beta"


def test_reload_on_dict_constructed_config_is_noop() -> None:
    cfg = Config({"app": {"name": "x"}})
    assert cfg.reload_from_sources() is False
    assert cfg.get("app.name") == "x"


@refresh_scope
class _FeatureReader:
    def __init__(self, config: Config) -> None:
        self.flag = config.get("feature.flag")


@pytest.mark.asyncio
async def test_refresh_reloads_config_for_refresh_scoped_bean(tmp_path: Path) -> None:
    cfg_file = tmp_path / "pyfly.yaml"
    cfg_file.write_text("feature:\n  flag: alpha\n")
    cfg = Config.from_sources(tmp_path, load_defaults=False)

    ctx = ApplicationContext(cfg)
    ctx.register_bean(_FeatureReader)
    await ctx.start()
    assert ctx.get_bean(_FeatureReader).flag == "alpha"

    # change the file on disk, then refresh -> config reloads + refresh-scoped bean rebuilds
    cfg_file.write_text("feature:\n  flag: beta\n")
    await ctx.get_bean(ContextRefresher).refresh()
    assert ctx.get_bean(_FeatureReader).flag == "beta"
