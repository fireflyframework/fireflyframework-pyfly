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
"""Method-level security decorators using RequestContext."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.kernel.exceptions import ForbiddenException, UnauthorizedException
from pyfly.security.expression import evaluate_security_expression

F = TypeVar("F", bound=Callable[..., Any])


def _bind_args(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Map a call's positional/keyword arguments to parameter names for ``#param`` refs."""
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return {}


def _get_security_context() -> Any:
    """Retrieve the SecurityContext from the current RequestContext.

    Raises:
        UnauthorizedException: If no RequestContext or SecurityContext is available.
    """
    from pyfly.context.request_context import RequestContext

    req_ctx = RequestContext.current()
    if req_ctx is None or req_ctx.security_context is None:
        raise UnauthorizedException(
            "Authentication required",
            code="AUTH_REQUIRED",
        )
    return req_ctx.security_context


def _check_expression(
    expression: str,
    *,
    args: dict[str, Any] | None = None,
    return_object: Any = None,
) -> None:
    """Evaluate a security expression against the current SecurityContext.

    *args* binds ``#paramName`` references; *return_object* binds ``returnObject``.

    Raises:
        UnauthorizedException: If no SecurityContext is available.
        ForbiddenException: If the expression evaluates to False.
    """
    ctx = _get_security_context()
    if not evaluate_security_expression(expression, ctx, args=args, return_object=return_object):
        raise ForbiddenException(
            f"Access denied by expression: {expression}",
            code="FORBIDDEN",
        )


def pre_authorize(expression: str) -> Callable[[F], F]:
    """Decorator that checks a security expression BEFORE method execution.

    Reads the SecurityContext from ``RequestContext.current().security_context``.

    Args:
        expression: A security expression (e.g. ``"hasRole('ADMIN')"``,
            ``"isAuthenticated"``, ``"hasPermission('order:read')"``).

    Raises:
        UnauthorizedException: If no SecurityContext is available.
        ForbiddenException: If the expression evaluates to False.
    """

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check_expression(expression, args=_bind_args(func, args, kwargs))
                return await func(*args, **kwargs)

            async_wrapper.__pyfly_pre_authorize__ = expression  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            _check_expression(expression, args=_bind_args(func, args, kwargs))
            return func(*args, **kwargs)

        sync_wrapper.__pyfly_pre_authorize__ = expression  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]

    return decorator


def post_authorize(expression: str) -> Callable[[F], F]:
    """Decorator that checks a security expression AFTER method execution.

    The decorated method runs first; the security check is performed on its
    return.  If authorization fails the result is discarded and an exception
    is raised.

    Reads the SecurityContext from ``RequestContext.current().security_context``.

    Args:
        expression: A security expression (e.g. ``"hasRole('ADMIN')"``,
            ``"isAuthenticated"``, ``"hasPermission('order:read')"``).

    Raises:
        UnauthorizedException: If no SecurityContext is available.
        ForbiddenException: If the expression evaluates to False.
    """

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                result = await func(*args, **kwargs)
                _check_expression(expression, args=_bind_args(func, args, kwargs), return_object=result)
                return result

            async_wrapper.__pyfly_post_authorize__ = expression  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            _check_expression(expression, args=_bind_args(func, args, kwargs), return_object=result)
            return result

        sync_wrapper.__pyfly_post_authorize__ = expression  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]

    return decorator
