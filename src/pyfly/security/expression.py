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
"""Method-security expression language — the Spring Security SpEL subset.

Evaluates ``@pre_authorize`` / ``@post_authorize`` / ``@secure(expression=...)``
expressions against the current :class:`SecurityContext`. Supports the security
vocabulary (``hasRole``/``hasAnyRole``/``hasAuthority``/``hasAnyAuthority``/
``hasPermission``/``isAuthenticated``/``isAnonymous``/``permitAll``/``denyAll`` — each
usable bare or called), the ``principal`` and ``authentication`` references, method
argument references (``#paramName``), ``returnObject`` (post-authorize), boolean
operators, comparisons, and (non-dunder) attribute/index access.

Safe by construction: the expression is parsed with ``ast`` and evaluated against a
whitelist of node types and a fixed namespace — ``eval`` is never used, only the
security functions are callable, and attribute names beginning with ``_`` are rejected.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable
from typing import Any

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.context import SecurityContext

_PARAM_RE = re.compile(r"#(\w+)")
_PARAM_PREFIX = "_pyfly_arg_"

_CMP_OPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class _BoolFn:
    """A security function usable both bare (``isAuthenticated``) and called
    (``isAuthenticated()``); ``bool()`` invokes it with no arguments."""

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[..., bool]) -> None:
        self._fn = fn

    def __call__(self, *args: Any) -> bool:
        return bool(self._fn(*args))

    def __bool__(self) -> bool:
        return bool(self._fn())


def _has_authority(ctx: SecurityContext, authority: Any) -> bool:
    name = str(authority)
    return ctx.has_role(name) or ctx.has_permission(name)


def _build_namespace(ctx: SecurityContext, args: dict[str, Any] | None, return_object: Any) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "principal": ctx,
        "authentication": ctx,
        "True": True,
        "False": False,
        "None": None,
        "true": True,
        "false": False,
        "null": None,
        "permitAll": _BoolFn(lambda: True),
        "denyAll": _BoolFn(lambda: False),
        "isAuthenticated": _BoolFn(lambda: ctx.is_authenticated),
        "isAnonymous": _BoolFn(lambda: not ctx.is_authenticated),
        "hasRole": _BoolFn(lambda role: ctx.has_role(str(role))),
        "hasAnyRole": _BoolFn(lambda *roles: ctx.has_any_role([str(r) for r in roles])),
        "hasAuthority": _BoolFn(lambda authority: _has_authority(ctx, authority)),
        "hasAnyAuthority": _BoolFn(lambda *auths: any(_has_authority(ctx, a) for a in auths)),
        # 1-arg hasPermission(perm) or 2-arg hasPermission(target, perm) — the last
        # argument is the permission (target-based ACLs are not modelled).
        "hasPermission": _BoolFn(lambda *parts: ctx.has_permission(str(parts[-1]))),
        "returnObject": return_object,
    }
    for key, value in (args or {}).items():
        namespace[_PARAM_PREFIX + key] = value
    return namespace


def _eval(node: ast.AST, ns: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ns)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _eval(value, ns)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = _eval(value, ns)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, ns)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ns)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, ns)
            func = _CMP_OPS.get(type(op))
            if func is None or not func(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        raise SecurityException(f"Unknown identifier in security expression: {node.id!r}", code="INVALID_EXPRESSION")
    if isinstance(node, ast.Call):
        func = _eval(node.func, ns)
        if not isinstance(func, _BoolFn):
            raise SecurityException("Only security functions may be called", code="INVALID_EXPRESSION")
        return func(*[_eval(a, ns) for a in node.args])
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise SecurityException(
                f"Access to private attribute {node.attr!r} is not allowed", code="INVALID_EXPRESSION"
            )
        return getattr(_eval(node.value, ns), node.attr)
    if isinstance(node, ast.Subscript):
        return _eval(node.value, ns)[_eval(node.slice, ns)]
    if isinstance(node, ast.List):
        return [_eval(e, ns) for e in node.elts]
    raise SecurityException(f"Unsafe security expression element: {type(node).__name__}", code="INVALID_EXPRESSION")


def evaluate_security_expression(
    expression: str,
    ctx: SecurityContext,
    *,
    args: dict[str, Any] | None = None,
    return_object: Any = None,
) -> bool:
    """Evaluate a method-security expression; returns the boolean decision."""
    translated = _PARAM_RE.sub(lambda m: _PARAM_PREFIX + m.group(1), expression.strip())
    try:
        tree = ast.parse(translated, mode="eval")
    except SyntaxError as exc:
        raise SecurityException(f"Invalid security expression syntax: {exc}", code="INVALID_EXPRESSION") from exc
    return bool(_eval(tree, _build_namespace(ctx, args, return_object)))
