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
"""Security decorators for role-based and permission-based access control."""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.kernel.exceptions import ForbiddenException, SecurityException
from pyfly.security.context import SecurityContext

F = TypeVar("F", bound=Callable[..., Any])


def _parse_string_args(args_str: str) -> list[str]:
    """Parse quoted string arguments from a function call argument string.

    Args:
        args_str: Raw argument string, e.g. ``"'ADMIN', 'MANAGER'"``.

    Returns:
        List of extracted string values.
    """
    return re.findall(r"'([^']+)'", args_str)


def _eval_node(node: ast.AST) -> bool:
    """Recursively evaluate a safe boolean AST node.

    Only ``ast.Constant``, ``ast.BoolOp``, and ``ast.UnaryOp(Not)`` nodes are
    permitted.  Any other node type causes a :class:`SecurityException`.

    Args:
        node: An AST node produced by :func:`ast.parse`.

    Returns:
        The boolean result of the expression.

    Raises:
        SecurityException: If an unsafe AST node is encountered.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v) for v in node.values)
        # ast.Or
        return any(_eval_node(v) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand)
    raise SecurityException(
        f"Unsafe expression node: {type(node).__name__}",
        code="INVALID_EXPRESSION",
    )


def _safe_bool_eval(expr: str) -> bool:
    """Evaluate a boolean expression containing only safe tokens.

    The expression must consist solely of ``True``, ``False``, ``and``, ``or``,
    ``not``, and parentheses.  The function parses the expression into an AST,
    validates that every node is a safe boolean construct, then evaluates it
    via recursive descent -- no ``eval()`` or ``exec()`` is used.

    Args:
        expr: A boolean expression string (e.g. ``"True and (False or True)"``).

    Returns:
        The boolean result.

    Raises:
        SecurityException: If the expression contains unsafe constructs.
    """
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise SecurityException(
            f"Invalid security expression syntax: {exc}",
            code="INVALID_EXPRESSION",
        ) from exc

    for node in ast.walk(tree):
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.BoolOp,
                ast.UnaryOp,
                ast.Constant,
                ast.And,
                ast.Or,
                ast.Not,
            ),
        ):
            raise SecurityException(
                f"Unsafe expression node: {type(node).__name__}",
                code="INVALID_EXPRESSION",
            )

    return _eval_node(tree.body)


def _evaluate_expression(expr: str, ctx: SecurityContext) -> bool:
    """Evaluate a security expression against a :class:`SecurityContext`.

    Supported constructs:

    - ``hasRole('ADMIN')`` -- checks ``ctx.has_role("ADMIN")``
    - ``hasAnyRole('ADMIN', 'MANAGER')`` -- checks ``ctx.has_any_role(...)``
    - ``hasPermission('user:read')`` -- checks ``ctx.has_permission(...)``
    - ``isAuthenticated`` -- checks ``ctx.is_authenticated``
    - Boolean operators: ``and``, ``or``, ``not``
    - Parentheses for grouping

    The function replaces each construct with its boolean result (``True`` or
    ``False``), validates that only safe tokens remain, and evaluates the
    resulting boolean expression via AST walking -- **no** ``eval()`` or
    ``exec()`` is used.

    Args:
        expr: The security expression string.
        ctx: The current request's security context.

    Returns:
        ``True`` if the expression evaluates to ``True``, ``False`` otherwise.

    Raises:
        SecurityException: If the expression contains invalid or unsafe tokens.
    """
    result = expr

    # Replace function calls with their boolean results.
    # hasAnyRole must be matched before hasRole to avoid partial matches.
    result = re.sub(
        r"hasAnyRole\(([^)]+)\)",
        lambda m: str(ctx.has_any_role(_parse_string_args(m.group(1)))),
        result,
    )
    result = re.sub(
        r"hasRole\('([^']+)'\)",
        lambda m: str(ctx.has_role(m.group(1))),
        result,
    )
    result = re.sub(
        r"hasPermission\('([^']+)'\)",
        lambda m: str(ctx.has_permission(m.group(1))),
        result,
    )
    result = result.replace("isAuthenticated", str(ctx.is_authenticated))

    # Validate that only safe tokens remain.
    safe_tokens = {"True", "False", "and", "or", "not", "(", ")"}
    cleaned = result.replace("(", " ( ").replace(")", " ) ")
    tokens = cleaned.split()
    if not all(t in safe_tokens for t in tokens):
        raise SecurityException(
            f"Invalid security expression: {expr}",
            code="INVALID_EXPRESSION",
        )

    return _safe_bool_eval(cleaned)


def secure(
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
    expression: str | None = None,
) -> Callable[[F], F]:
    """Decorator for securing endpoints with role and permission checks.

    The decorated function must accept a ``security_context`` keyword argument
    of type :class:`SecurityContext`.

    Args:
        roles: Required roles (user must have at least one).
        permissions: Required permissions (user must have all).
        expression: A security expression evaluated against the
            :class:`SecurityContext`.  Supports ``hasRole``, ``hasAnyRole``,
            ``hasPermission``, ``isAuthenticated``, and boolean operators
            (``and``, ``or``, ``not``) with parenthesised grouping.

    Raises:
        SecurityException: If authentication or authorization fails.

    Usage::

        @secure(roles=["ADMIN"], permissions=["order:delete"])
        async def delete_order(order_id: str, security_context: SecurityContext) -> None: ...

        @secure(expression="hasRole('ADMIN') and hasPermission('order:delete')")
        async def delete_order(order_id: str, security_context: SecurityContext) -> None: ...
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx: SecurityContext | None = kwargs.get("security_context")
            if ctx is None:
                raise SecurityException("Authentication required", code="AUTH_REQUIRED")

            if not ctx.is_authenticated:
                raise SecurityException("Authentication required", code="AUTH_REQUIRED")

            if roles and not ctx.has_any_role(roles):
                raise ForbiddenException(
                    f"Insufficient roles: requires one of {roles}",
                    code="FORBIDDEN",
                )

            if permissions:
                missing = [p for p in permissions if not ctx.has_permission(p)]
                if missing:
                    raise ForbiddenException(
                        f"Insufficient permissions: missing {missing}",
                        code="FORBIDDEN",
                    )

            if expression and not _evaluate_expression(expression, ctx):
                raise ForbiddenException(
                    f"Access denied by expression: {expression}",
                    code="FORBIDDEN",
                )

            return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
