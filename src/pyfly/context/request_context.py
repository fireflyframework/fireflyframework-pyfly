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
"""Request-scoped context backed by contextvars.

Each HTTP request gets a fresh RequestContext via RequestContextFilter.
The context stores request_id, security_context, and arbitrary attributes.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from pyfly.security.context import SecurityContext

_request_context_var: ContextVar[RequestContext | None] = ContextVar("pyfly_request_context", default=None)

# Attribute key under which the SessionFilter stashes the active HttpSession, so the
# container can resolve SESSION-scoped beans from it.
HTTP_SESSION_KEY = "__pyfly_http_session__"


class RequestContext:
    """Holds per-request state: request ID, security context, and custom attributes.

    Use ``RequestContext.init()`` to create a new context for the current
    async task, and ``RequestContext.current()`` to retrieve it.
    """

    def __init__(self, request_id: str | None = None) -> None:
        self._request_id = request_id or uuid.uuid4().hex
        self._security_context: SecurityContext | None = None
        self._attributes: dict[str, Any] = {}

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def security_context(self) -> SecurityContext | None:
        return self._security_context

    @security_context.setter
    def security_context(self, value: SecurityContext | None) -> None:
        self._security_context = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._attributes.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._attributes[key] = value

    @classmethod
    def init(cls, request_id: str | None = None) -> RequestContext:
        """Create and set a new RequestContext for the current async task."""
        ctx = cls(request_id=request_id)
        _request_context_var.set(ctx)
        return ctx

    @classmethod
    def current(cls) -> RequestContext | None:
        """Get the RequestContext for the current async task, or None."""
        return _request_context_var.get()

    @classmethod
    def clear(cls) -> None:
        """Clear the RequestContext for the current async task."""
        _request_context_var.set(None)
