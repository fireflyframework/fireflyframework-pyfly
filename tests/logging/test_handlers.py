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

from pyfly.config.properties.logging import FileProperties, RollingProperties
from pyfly.logging.handlers import build_file_handler, parse_size


def test_parse_size():
    assert parse_size("10MB") == 10 * 1024 * 1024
    assert parse_size("512KB") == 512 * 1024
    assert parse_size("2GB") == 2 * 1024 * 1024 * 1024
    assert parse_size("4096") == 4096
    assert parse_size("") == 0


def test_build_file_handler(tmp_path: pathlib.Path):
    fp = FileProperties(name="app.log", path=str(tmp_path))
    handler = build_file_handler(fp, RollingProperties(max_size="1MB", max_history=3))
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 1024 * 1024
    assert handler.backupCount == 3
    assert pathlib.Path(handler.baseFilename) == (tmp_path / "app.log")
    handler.close()


def test_build_file_handler_none_without_name():
    assert build_file_handler(FileProperties(), RollingProperties()) is None
