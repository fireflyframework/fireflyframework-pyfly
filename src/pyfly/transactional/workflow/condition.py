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
"""Safe boolean expression evaluation for @workflow_step(condition=...).

A SpEL substitute: evaluates a restricted Python expression against a namespace
of workflow facts (``results``, ``variables``, ``headers``, ``input``). Only a
whitelist of AST nodes is permitted — no function calls, attribute writes,
comprehensions, lambdas, or name binding — so a condition string can never run
arbitrary code (unlike ``eval``). Audit #59.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


class ConditionError(ValueError):
    """Raised when a step condition expression is invalid or uses a banned node."""


def evaluate_condition(expression: str, namespace: dict[str, Any]) -> bool:
    """Evaluate *expression* to a bool against *namespace*.

    Raises :class:`ConditionError` if the expression cannot be parsed or uses a
    construct outside the safe whitelist.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"invalid condition {expression!r}: {exc}") from exc
    return bool(_eval(tree.body, namespace))


def _eval(node: ast.AST, ns: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        if node.id in ("True", "False", "None"):  # pragma: no cover - handled as Constant on 3.12
            return {"True": True, "False": False, "None": None}[node.id]
        raise ConditionError(f"unknown name in condition: {node.id!r}")
    if isinstance(node, ast.BoolOp):
        values = [_eval(v, ns) for v in node.values]
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in values:
                result = v
                if not v:
                    break
            return result
        last: Any = False
        for v in values:
            last = v
            if v:
                break
        return last
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, ns)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ConditionError(f"unsupported unary operator: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ConditionError(f"unsupported operator: {type(node.op).__name__}")
        return op(_eval(node.left, ns), _eval(node.right, ns))
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ns)
        for op_node, comparator in zip(node.ops, node.comparators, strict=False):
            cmp = _CMP_OPS.get(type(op_node))
            if cmp is None:
                raise ConditionError(f"unsupported comparison: {type(op_node).__name__}")
            right = _eval(comparator, ns)
            if not cmp(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Subscript):
        return _eval(node.value, ns)[_eval(node.slice, ns)]
    if isinstance(node, ast.Attribute):
        return getattr(_eval(node.value, ns), node.attr)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_eval(e, ns) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(items)
        if isinstance(node, ast.Set):
            return set(items)
        return items
    if isinstance(node, ast.Dict):
        return {_eval(k, ns): _eval(v, ns) for k, v in zip(node.keys, node.values, strict=False) if k is not None}
    raise ConditionError(f"disallowed expression element: {type(node).__name__}")
