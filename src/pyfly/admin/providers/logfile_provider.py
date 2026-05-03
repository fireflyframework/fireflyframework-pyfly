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
"""Logfile data provider -- log record listing and management."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.admin.log_handler import AdminLogHandler
    from pyfly.context.application_context import ApplicationContext


class LogfileProvider:
    """Provides log records from the in-memory AdminLogHandler.

    Uses lazy resolution from the ApplicationContext, following the same
    pattern as ``CacheProvider``.  This is necessary because ``create_app``
    runs at module-load time *before* ``ApplicationContext.start()``
    instantiates auto-configuration beans.
    """

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context
        self._handler: AdminLogHandler | None = None

    def _resolve_handler(self) -> AdminLogHandler | None:
        if self._handler is not None:
            return self._handler
        try:
            from pyfly.admin.log_handler import AdminLogHandler

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, AdminLogHandler):
                    self._handler = reg.instance
                    return self._handler
        except ImportError:
            pass
        return None

    @property
    def handler(self) -> AdminLogHandler | None:
        """Expose the resolved handler (used by SSE streams)."""
        return self._resolve_handler()

    async def get_logfile(self) -> dict[str, Any]:
        handler = self._resolve_handler()
        if handler is None:
            return {"available": False, "records": [], "total": 0}
        records = handler.get_all()
        return {
            "available": True,
            "records": records,
            "total": len(records),
        }

    async def clear_logfile(self) -> dict[str, Any]:
        handler = self._resolve_handler()
        if handler is None:
            return {"error": "Log handler not available"}
        handler.clear()
        return {"cleared": True}
