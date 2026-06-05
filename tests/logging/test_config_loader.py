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
from __future__ import annotations

import logging
import pathlib

from pyfly.logging.config_loader import apply_external_config


def test_apply_dictconfig_yaml(tmp_path: pathlib.Path):
    cfg = tmp_path / "logging.yaml"
    cfg.write_text(
        "version: 1\n"
        "disable_existing_loggers: false\n"
        "handlers:\n"
        "  console:\n"
        "    class: logging.StreamHandler\n"
        "root:\n"
        "  level: WARNING\n"
        "  handlers: [console]\n"
    )
    assert apply_external_config(str(cfg)) is True
    assert logging.getLogger().level == logging.WARNING


def test_apply_missing_returns_false():
    assert apply_external_config("/nonexistent/logging.yaml") is False


def test_apply_empty_path_returns_false():
    assert apply_external_config("") is False
