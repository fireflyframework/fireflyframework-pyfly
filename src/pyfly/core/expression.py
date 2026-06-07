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
"""A small, SAFE expression evaluator — the pyfly subset of Spring's SpEL.

Supports the ``#{ ... }`` form used by ``@Value`` and ``@ConditionalOnExpression``:
arithmetic, comparison, boolean (and/or/not), the ternary ``a if c else b``,
literals, lists/tuples, ``${key:default}`` config-placeholder substitution, and an
``env`` mapping for environment variables.

It is intentionally NOT full SpEL: there is no attribute access, no function/method
calls, no object navigation, no assignment — the expression is parsed with ``ast`` and
evaluated against a whitelist of node types, so it can never execute arbitrary code
(``eval`` is never used).
"""

from __future__ import annotations

import ast
import operator
import os
import re
from typing import Any

from pyfly.kernel.exceptions import PyFlyException

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")

_BIN_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMP_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class ExpressionError(PyFlyException):
    """Raised when an expression is malformed or uses an unsupported construct."""


def is_expression(text: str) -> bool:
    """Whether *text* is a ``#{ ... }`` expression."""
    text = text.strip()
    return text.startswith("#{") and text.endswith("}")


def _as_literal(value: Any) -> str:
    """Render *value* as a parseable expression literal.

    Real numbers and numeric-looking strings become bare numbers (so they take part in
    arithmetic); everything else is ``repr``-quoted.
    """
    if isinstance(value, bool) or value is None or isinstance(value, int | float):
        return repr(value)
    text = str(value)
    for caster in (int, float):
        try:
            caster(text)
            return text
        except ValueError:
            continue
    return repr(text)


def _substitute_placeholders(expr: str, config: Any) -> str:
    """Replace ``${key}`` / ``${key:default}`` with the resolved value as a literal."""

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if ":" in inner:
            key, default = inner.split(":", 1)
            value = config.get(key.strip()) if config is not None else None
            value = default if value is None else value
        else:
            value = config.get(inner.strip()) if config is not None else None
            if value is None:
                raise ExpressionError(f"Configuration key '{inner.strip()}' not found in expression")
        return _as_literal(value)

    return _PLACEHOLDER.sub(_replace, expr)


def _names() -> dict[str, Any]:
    return {
        "true": True,
        "false": False,
        "null": None,
        "True": True,
        "False": False,
        "None": None,
        "env": dict(os.environ),
    }


def _eval(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, names)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval(node.left, names), _eval(node.right, names))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand, names))
    if isinstance(node, ast.BoolOp):
        result: Any = isinstance(node.op, ast.And)
        for value_node in node.values:
            result = _eval(value_node, names)
            if isinstance(node.op, ast.And) and not result:
                return result
            if isinstance(node.op, ast.Or) and result:
                return result
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, names)
            if type(op) not in _CMP_OPS or not _CMP_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, names) if _eval(node.test, names) else _eval(node.orelse, names)
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise ExpressionError(f"Unknown name in expression: {node.id!r}")
    if isinstance(node, ast.Subscript):
        return _eval(node.value, names)[_eval(node.slice, names)]
    if isinstance(node, ast.List):
        return [_eval(element, names) for element in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(element, names) for element in node.elts)
    raise ExpressionError(f"Unsupported expression element: {type(node).__name__}")


def evaluate(text: str, config: Any = None) -> Any:
    """Evaluate a ``#{ ... }`` expression safely; returns the computed value.

    ``${key:default}`` placeholders are substituted from *config* before evaluation.
    """
    stripped = text.strip()
    inner = stripped[2:-1] if is_expression(stripped) else stripped
    substituted = _substitute_placeholders(inner, config)
    try:
        tree = ast.parse(substituted, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression: {text!r} ({exc.msg})") from exc
    return _eval(tree, _names())
