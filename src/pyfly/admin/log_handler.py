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
"""In-memory log handler for the admin dashboard log viewer."""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import UTC, datetime
from typing import Any

# Strip ANSI escape codes produced by structlog's ConsoleRenderer
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Parse structlog console format:
#   TIMESTAMP [level    ] event_name                     [logger] key=value ...
_STRUCTLOG_RE = re.compile(r"^(\S+)\s+\[(\w+)\s*\]\s+(.*?)\s+\[([^\]]+)\]\s*(.*)$")


class AdminLogHandler(logging.Handler):
    """Captures log records in a ring buffer for the admin log viewer.

    Stores formatted log records in a fixed-size deque, following the same
    ring buffer pattern as ``TraceCollectorFilter``.
    """

    def __init__(self, max_records: int = 2000) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=max_records)
        self._counter: int = 0

    @staticmethod
    def _parse_message(raw: str) -> dict[str, str]:
        """Strip ANSI codes and extract structlog event/context if possible."""
        clean = _ANSI_RE.sub("", raw).strip()
        m = _STRUCTLOG_RE.match(clean)
        if m:
            return {
                "event": m.group(3).strip(),
                "context": m.group(5).strip(),
            }
        return {"event": clean, "context": ""}

    def emit(self, record: logging.LogRecord) -> None:
        self._counter += 1
        parsed = self._parse_message(self.format(record))
        self._records.append(
            {
                "id": self._counter,
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": parsed["event"],
                "context": parsed["context"],
                "thread": record.threadName,
            }
        )

    def get_records(self, after: int = 0) -> list[dict[str, Any]]:
        """Return records with id > after (for incremental SSE polling)."""
        return [r for r in self._records if r["id"] > after]

    def get_all(self) -> list[dict[str, Any]]:
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()
