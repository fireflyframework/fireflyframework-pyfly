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
"""Load an external logging config file (dictConfig YAML / fileConfig INI)."""

from __future__ import annotations

import logging
import logging.config
import pathlib

_logger = logging.getLogger("pyfly.logging")


def apply_external_config(path: str) -> bool:
    """Apply an external logging config; return True if it was applied.

    ``*.yaml`` / ``*.yml`` / ``*.json`` are loaded as ``logging.config.dictConfig``;
    ``*.ini`` / ``*.conf`` via ``logging.config.fileConfig``. Failures are logged
    and return False (the adapter then falls back to its inline configuration).
    """
    if not path:
        return False
    file = pathlib.Path(path)
    if not file.is_file():
        _logger.warning("logging config file not found: %s", path)
        return False
    try:
        suffix = file.suffix.lower()
        if suffix in (".yaml", ".yml"):
            import yaml  # type: ignore[import-untyped]

            logging.config.dictConfig(yaml.safe_load(file.read_text(encoding="utf-8")) or {})
        elif suffix == ".json":
            import json

            logging.config.dictConfig(json.loads(file.read_text(encoding="utf-8")))
        else:
            logging.config.fileConfig(str(file), disable_existing_loggers=False)
    except Exception as exc:  # noqa: BLE001 — bad config must not crash startup
        _logger.warning("failed to apply logging config %s: %s", path, exc)
        return False
    return True
