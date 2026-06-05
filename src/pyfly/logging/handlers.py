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
"""File-appender construction with size-based rotation."""

from __future__ import annotations

import logging
import logging.handlers
import pathlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyfly.config.properties.logging import FileProperties, RollingProperties

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_size(value: str) -> int:
    """Parse a human size like ``10MB`` / ``512KB`` / ``4096`` to bytes (0 if empty/invalid)."""
    if not value:
        return 0
    match = _SIZE_RE.match(value)
    if not match:
        return 0
    number, unit = match.groups()
    unit = unit.upper()
    if unit and not unit.endswith("B"):
        unit += "B"
    return int(float(number) * _UNITS.get(unit, 1))


def build_file_handler(
    file_props: FileProperties,
    rolling: RollingProperties,
) -> logging.Handler | None:
    """Build a rotating file handler, or None when no file name is configured."""
    if not file_props.name:
        return None
    directory = pathlib.Path(file_props.path) if file_props.path else pathlib.Path()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / file_props.name
    max_bytes = parse_size(rolling.max_size)
    return logging.handlers.RotatingFileHandler(
        filename=str(target),
        maxBytes=max_bytes,
        backupCount=max(0, rolling.max_history),
        encoding="utf-8",
    )
