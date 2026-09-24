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
"""ActuatorEndpoint protocol — extensible actuator endpoint interface."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

F = TypeVar("F", bound=Callable[..., Any])


@runtime_checkable
class ActuatorEndpoint(Protocol):
    """Protocol for actuator management endpoints.

    Each endpoint is exposed at ``/actuator/{endpoint_id}``.
    Implement this protocol directly or as a ``@component`` bean for
    auto-discovery.
    """

    @property
    def endpoint_id(self) -> str:
        """URL path suffix: ``/actuator/{endpoint_id}``."""
        ...

    @property
    def enabled(self) -> bool:
        """Default enable state.  Can be overridden via config."""
        ...

    async def handle(self, context: Any = None) -> dict[str, Any] | None:
        """Handle a request and return a JSON-serializable dict.

        May return ``None`` for a selector that matches nothing (e.g.
        ``/actuator/metrics/{unknown}``), which the adapter renders as 404.
        """
        ...


def write_operation(func: F) -> F:
    """Mark the one method of an :class:`ActuatorEndpoint` that answers ``POST``.

    The Spring ``@WriteOperation`` equivalent. The adapter mounts the marked method as
    ``POST /actuator/{endpoint_id}`` (and ``POST /actuator/{endpoint_id}/{selector}`` when the
    endpoint sets ``supports_selector``) and calls it as::

        async def method(self, body: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None

    ``body`` is the request's JSON object (a missing body, a body that is not JSON, or JSON that
    is not an object is refused with 400 before the method runs); ``context`` is the same
    ``{"query": ..., "selector": ...}`` the read operation receives. Return ``None`` for
    ``204 No Content``, a dict for ``200`` with that document, or a dict carrying ``"error"``
    for ``400`` with it — the convention the built-in loggers endpoint already follows.

    Until this marker existed a custom endpoint was GET-only: ``_make_generic_routes`` mounted
    nothing else, so any management command that took a document had to bypass the actuator.
    """
    func.__pyfly_write_operation__ = True  # type: ignore[attr-defined]
    return func


def find_write_operation(endpoint: object) -> Callable[..., Any] | None:
    """The bound ``@write_operation`` method of *endpoint*, or ``None``.

    An endpoint carries at most one: two would map to the same ``POST`` path and the adapter
    would have to pick, silently. Raised as ``TypeError`` because it is a definition error.
    """
    found: list[Callable[..., Any]] = []
    for name, member in inspect.getmembers(type(endpoint), predicate=inspect.isfunction):
        if getattr(member, "__pyfly_write_operation__", False):
            found.append(getattr(endpoint, name))
    if len(found) > 1:
        msg = (
            f"{type(endpoint).__name__} declares {len(found)} write operations; "
            "an endpoint carries one @write_operation"
        )
        raise TypeError(msg)
    return found[0] if found else None
