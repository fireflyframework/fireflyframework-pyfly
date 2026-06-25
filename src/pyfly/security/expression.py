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
from pyfly.security.permission import PermissionEvaluator
from pyfly.security.role_hierarchy import RoleHierarchy

_PARAM_RE = re.compile(r"#(\w+)")
_PARAM_PREFIX = "_pyfly_arg_"

# Process-wide role hierarchy consulted by hasRole/hasAnyRole/hasAuthority (Spring's
# RoleHierarchy bean). Configure once at startup via set_role_hierarchy().
_active_hierarchy: RoleHierarchy | None = None

# Process-wide PermissionEvaluator backing hasPermission(target, perm). When unset,
# hasPermission falls back to a flat permission check on the SecurityContext.
_active_permission_evaluator: PermissionEvaluator | None = None


def set_role_hierarchy(hierarchy: RoleHierarchy | None) -> None:
    """Install the role hierarchy used by method-security role checks (``None`` disables)."""
    global _active_hierarchy
    _active_hierarchy = hierarchy


def get_role_hierarchy() -> RoleHierarchy | None:
    """Return the currently installed role hierarchy, if any."""
    return _active_hierarchy


def set_permission_evaluator(evaluator: PermissionEvaluator | None) -> None:
    """Install the PermissionEvaluator used by ``hasPermission`` (``None`` disables)."""
    global _active_permission_evaluator
    _active_permission_evaluator = evaluator


def get_permission_evaluator() -> PermissionEvaluator | None:
    """Return the currently installed PermissionEvaluator, if any."""
    return _active_permission_evaluator


def _eval_permission(ctx: SecurityContext, parts: tuple[Any, ...]) -> bool:
    """Resolve a ``hasPermission(...)`` call against the evaluator or the context.

    Argument shapes (Spring parity):
      * ``(permission,)``                    — flat permission check
      * ``(target, permission)``             — domain-object permission
      * ``(target_id, target_type, perm)``   — identifier + type permission
    """
    if not parts:
        return False
    evaluator = _active_permission_evaluator
    if evaluator is None:
        # No ACL evaluator: fall back to the principal's flat permissions.
        return ctx.has_permission(str(parts[-1]))
    if len(parts) == 1:
        return evaluator.has_permission(ctx, None, str(parts[0]))
    if len(parts) == 2:
        return evaluator.has_permission(ctx, parts[0], str(parts[1]))
    target_id, target_type, permission = parts[-3], parts[-2], parts[-1]
    return evaluator.has_permission(ctx, target_id, str(permission), target_type=str(target_type))


def _effective_roles(ctx: SecurityContext) -> set[str]:
    """The principal's roles, expanded through the active hierarchy when one is set."""
    if _active_hierarchy is None:
        return set(ctx.roles)
    return _active_hierarchy.expand(ctx.roles)


def _has_role(ctx: SecurityContext, role: Any) -> bool:
    name = str(role)
    return ctx.has_role(name) if _active_hierarchy is None else name in _effective_roles(ctx)


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
    return _has_role(ctx, name) or ctx.has_permission(name)


def _build_namespace(
    ctx: SecurityContext, args: dict[str, Any] | None, return_object: Any, filter_object: Any = None
) -> dict[str, Any]:
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
        "hasRole": _BoolFn(lambda role: _has_role(ctx, role)),
        "hasAnyRole": _BoolFn(lambda *roles: any(_has_role(ctx, r) for r in roles)),
        "hasAuthority": _BoolFn(lambda authority: _has_authority(ctx, authority)),
        "hasAnyAuthority": _BoolFn(lambda *auths: any(_has_authority(ctx, a) for a in auths)),
        # hasPermission(perm) / (target, perm) / (id, type, perm) — dispatched to the
        # installed PermissionEvaluator, or a flat context check when none is set.
        "hasPermission": _BoolFn(lambda *parts: _eval_permission(ctx, parts)),
        "returnObject": return_object,
        "filterObject": filter_object,
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
    filter_object: Any = None,
) -> bool:
    """Evaluate a method-security expression; returns the boolean decision.

    *filter_object* binds ``filterObject`` for ``@pre_filter`` / ``@post_filter``.
    """
    translated = _PARAM_RE.sub(lambda m: _PARAM_PREFIX + m.group(1), expression.strip())
    try:
        tree = ast.parse(translated, mode="eval")
    except SyntaxError as exc:
        raise SecurityException(f"Invalid security expression syntax: {exc}", code="INVALID_EXPRESSION") from exc
    return bool(_eval(tree, _build_namespace(ctx, args, return_object, filter_object)))
