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
"""HTTP idempotency support — ``@disable_idempotency`` marker decorator.

The :func:`disable_idempotency` decorator sets a sentinel attribute on the
handler function that :class:`~pyfly.web.adapters.starlette.filters.idempotency_filter.IdempotencyWebFilter`
checks when deciding whether to cache a response.  It is framework-agnostic
and does not import any web-server library.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

#: Attribute name written onto handler functions by :func:`disable_idempotency`.
DISABLE_IDEMPOTENCY_ATTR: str = "__pyfly_disable_idempotency__"


def disable_idempotency(func: F) -> F:
    """Mark a route handler so the idempotency filter never caches its responses.

    Usage::

        @post_mapping("/payment")
        @disable_idempotency
        async def create_payment(self, ...) -> ...:
            ...

    The :class:`~pyfly.web.adapters.starlette.filters.idempotency_filter.IdempotencyWebFilter`
    inspects ``request.scope.get("endpoint")`` (Starlette's matched-endpoint
    slot) for this attribute.  If present and truthy the filter passes the
    request straight through without consulting the cache and without storing
    the response.
    """
    setattr(func, DISABLE_IDEMPOTENCY_ATTR, True)
    return func
