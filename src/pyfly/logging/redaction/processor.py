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
"""structlog redaction processor + stdlib RedactionFilter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pyfly.logging.redaction.engine import Redactor

_REDACTED = "<REDACTED>"


def make_structlog_redactor(
    redactor: Redactor,
    allow_fields: list[str],
    deny_fields: list[str],
) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
    """Build a structlog processor that redacts the event + string fields."""
    allow = set(allow_fields)
    deny = set(deny_fields)

    def processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in list(event_dict.items()):
            if key in deny:
                event_dict[key] = _REDACTED
                continue
            if allow and key not in allow and key != "event":
                continue
            if isinstance(value, str):
                event_dict[key] = redactor.redact(value)
        return event_dict

    return processor


class RedactionFilter(logging.Filter):
    """Stdlib logging filter that redacts the rendered message of every record.

    Attached to handlers so framework AND third-party records are covered.
    """

    def __init__(self, redactor: Redactor, allow_fields: list[str], deny_fields: list[str]) -> None:
        super().__init__()
        self._redactor = redactor
        self._deny = set(deny_fields)
        # ``allow_fields`` is accepted for call-site symmetry with
        # ``make_structlog_redactor`` but is intentionally unused: this filter
        # redacts the fully-rendered message string, where per-field scoping has
        # no meaning. Field-level allow-listing applies only on the structlog path.
        _ = allow_fields

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never drop a log line due to formatting
            return True
        redacted = self._redactor.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        # Redact string extras whose key is denied (e.g. extra={"password": ...}).
        for key in self._deny:
            if isinstance(record.__dict__.get(key), str):
                record.__dict__[key] = _REDACTED
        return True
