# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""FastAPI request ownership enclosing the complete user middleware stack."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from pyfly.web.adapters.starlette.context_bound import application_registry_scope

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class ContextBoundFastAPI(FastAPI):
    """Retain FastAPI's public surface and bind transaction resolution for the full ASGI response."""

    def __init__(self, *args: Any, pyfly_context: ApplicationContext | None = None, **kwargs: Any) -> None:
        self._pyfly_context = pyfly_context
        super().__init__(*args, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with application_registry_scope(self._pyfly_context, scope):
            await super().__call__(scope, receive, send)
