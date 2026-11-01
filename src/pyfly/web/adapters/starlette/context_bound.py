# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Application ownership outside the complete ASGI middleware stack."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.types import Receive, Scope, Send

from pyfly.container.exceptions import NoSuchBeanError
from pyfly.data.transaction.registry import TransactionManagerRegistry, bind_registry

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


@contextmanager
def application_registry_scope(context: ApplicationContext | None, scope: Scope) -> Iterator[None]:
    if context is None or scope["type"] not in ("http", "websocket"):
        yield
        return
    try:
        registry = context.get_bean(TransactionManagerRegistry)
    except NoSuchBeanError as error:
        if error.dependency_failure:
            raise
        registry = None
    with bind_registry(registry):
        yield


class ContextBoundStarlette(Starlette):
    """Retain Starlette's public surface while enclosing middleware added after app creation."""

    def __init__(self, *args: Any, pyfly_context: ApplicationContext | None = None, **kwargs: Any) -> None:
        self._pyfly_context = pyfly_context
        super().__init__(*args, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with application_registry_scope(self._pyfly_context, scope):
            await super().__call__(scope, receive, send)
