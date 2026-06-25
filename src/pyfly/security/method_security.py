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


_COLLECTION_TYPES = (list, tuple, set)


def _filter_collection(expression: str, collection: Any, args: dict[str, Any]) -> Any:
    """Return *collection* with only the elements for which *expression* (bound to
    ``filterObject``) is True, preserving the collection's concrete type."""
    ctx = _get_security_context()
    kept = [
        item
        for item in collection
        if evaluate_security_expression(expression, ctx, args=args, filter_object=item)
    ]
    return type(collection)(kept)


def _first_collection_param(arguments: dict[str, Any]) -> str | None:
    """Name of the first argument (skipping ``self``/``cls``) holding a collection."""
    for name, value in arguments.items():
        if name in ("self", "cls"):
            continue
        if isinstance(value, _COLLECTION_TYPES):
            return name
    return None


def pre_filter(expression: str, filter_target: str | None = None) -> Callable[[F], F]:
    """Filter a collection *argument* before the method runs (Spring ``@PreFilter``).

    Each element is bound to ``filterObject``; elements for which *expression* is
    False are removed. ``filter_target`` names the collection parameter; when
    omitted, the first collection-valued argument is used.
    """

    def decorator(func: F) -> F:
        signature = inspect.signature(func)

        def _filtered_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            target = filter_target or _first_collection_param(bound.arguments)
            if target is None or target not in bound.arguments:
                return args, kwargs
            collection = bound.arguments[target]
            if not isinstance(collection, _COLLECTION_TYPES):
                return args, kwargs
            bound.arguments[target] = _filter_collection(expression, collection, dict(bound.arguments))
            return bound.args, bound.kwargs

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                new_args, new_kwargs = _filtered_call(args, kwargs)
                return await func(*new_args, **new_kwargs)

            async_wrapper.__pyfly_pre_filter__ = expression  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            new_args, new_kwargs = _filtered_call(args, kwargs)
            return func(*new_args, **new_kwargs)

        sync_wrapper.__pyfly_pre_filter__ = expression  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]

    return decorator


def post_filter(expression: str) -> Callable[[F], F]:
    """Filter the returned collection after the method runs (Spring ``@PostFilter``).

    Each returned element is bound to ``filterObject``; non-collection results are
    returned unchanged.
    """

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                result = await func(*args, **kwargs)
                if not isinstance(result, _COLLECTION_TYPES):
                    return result
                return _filter_collection(expression, result, _bind_args(func, args, kwargs))

            async_wrapper.__pyfly_post_filter__ = expression  # type: ignore[attr-defined]
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            if not isinstance(result, _COLLECTION_TYPES):
                return result
            return _filter_collection(expression, result, _bind_args(func, args, kwargs))

        sync_wrapper.__pyfly_post_filter__ = expression  # type: ignore[attr-defined]
        return sync_wrapper  # type: ignore[return-value]

    return decorator


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
