# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Pure-python rule evaluator — runs over an evaluation context dict."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pyfly.rule_engine.dsl import Action, Condition, Rule, RuleSet

#: Type alias for action-handler callables stored in the internal registry.
_HandlerFn = Callable[[Action, dict[str, Any]], None]


def _make_default_handlers() -> dict[str, _HandlerFn]:
    """Build the default action-handler registry (``set``, ``increment``, ``log``)."""

    def _handle_set(action: Action, ctx: dict[str, Any]) -> None:
        if action.target is None:
            msg = "set action missing 'target'"
            raise ValueError(msg)
        RuleEvaluator._write(action.target, action.value, ctx)

    def _handle_increment(action: Action, ctx: dict[str, Any]) -> None:
        if action.target is None:
            msg = "increment action missing 'target'"
            raise ValueError(msg)
        current = RuleEvaluator._read(action.target, ctx) or 0
        RuleEvaluator._write(action.target, current + (action.value or 1), ctx)

    def _handle_log(action: Action, ctx: dict[str, Any]) -> None:
        logging.getLogger(__name__).info("rule action: %s", action.value or action.target)

    return {
        "set": _handle_set,
        "increment": _handle_increment,
        "log": _handle_log,
    }


@dataclass
class EvaluationResult:
    rule_id: str
    matched: bool
    actions_executed: list[Action] = field(default_factory=list)
    error: str | None = None


class RuleEvaluator:
    """Single-rule evaluator with a pluggable action-handler registry.

    Parameters
    ----------
    action_handlers:
        Optional mapping of action-type strings to handler callables.  Values
        are merged on top of the built-in ``set`` / ``increment`` / ``log``
        handlers, so you can override builtins or add entirely new types (e.g.
        ``"call"``, ``"http"``) without subclassing.  Any action type not found
        in the final registry raises :exc:`NotImplementedError`, matching the
        original loud-failure semantics (audit #215).
    """

    def __init__(
        self,
        action_handlers: dict[str, _HandlerFn] | None = None,
    ) -> None:
        self._handlers: dict[str, _HandlerFn] = _make_default_handlers()
        if action_handlers:
            self._handlers.update(action_handlers)

    def evaluate(self, rule: Rule, ctx: dict[str, Any]) -> EvaluationResult:
        if not rule.enabled:
            return EvaluationResult(rule_id=rule.id, matched=False)
        try:
            matched = self._eval_condition(rule.when, ctx) if rule.when else True
        except Exception as exc:  # noqa: BLE001
            return EvaluationResult(rule_id=rule.id, matched=False, error=str(exc))
        actions = rule.then if matched else rule.otherwise
        executed: list[Action] = []
        errors: list[str] = []
        for action in actions:
            # Isolate each action: one failing action records its error and the
            # rest still run, matching Java's isolate-and-continue (audit #216).
            try:
                self._execute_action(action, ctx)
                executed.append(action)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{action.type}: {exc}")
        return EvaluationResult(
            rule_id=rule.id,
            matched=matched,
            actions_executed=executed,
            error="; ".join(errors) or None,
        )

    # -- internals ---------------------------------------------------------

    def _eval_condition(self, c: Condition | None, ctx: dict[str, Any]) -> bool:
        if c is None:
            return True
        op = c.operator
        if op == "and":
            return all(self._eval_condition(child, ctx) for child in c.children)
        if op == "or":
            return any(self._eval_condition(child, ctx) for child in c.children)
        if op == "not":
            # 'not' must negate exactly one child; empty/multiple children are
            # malformed and would otherwise be silently ignored (audit #222).
            if len(c.children) != 1:
                msg = f"'not' requires exactly one child, got {len(c.children)}"
                raise ValueError(msg)
            return not self._eval_condition(c.children[0], ctx)
        actual = self._read(c.field, ctx) if c.field else None
        expected = c.value
        if op == "eq":
            return bool(actual == expected)
        if op == "ne":
            return bool(actual != expected)
        if op == "gt":
            return actual is not None and actual > expected
        if op == "ge":
            return actual is not None and actual >= expected
        if op == "lt":
            return actual is not None and actual < expected
        if op == "le":
            return actual is not None and actual <= expected
        if op == "in":
            return actual in (expected or [])
        if op == "not_in":
            return actual not in (expected or [])
        if op == "regex":
            return bool(re.search(str(expected), str(actual or "")))
        if op == "between":
            if actual is None:
                return False
            lo, hi = expected[0], expected[1]
            return bool(lo <= actual <= hi)
        if op == "contains":
            if actual is None:
                return False
            if isinstance(actual, str):
                return str(expected) in actual
            return expected in actual
        if op == "not_contains":
            if actual is None:
                return False
            if isinstance(actual, str):
                return str(expected) not in actual
            return expected not in actual
        if op == "starts_with":
            if actual is None:
                return False
            return str(actual).startswith(str(expected))
        if op == "ends_with":
            if actual is None:
                return False
            return str(actual).endswith(str(expected))
        if op == "exists":
            return actual is not None
        if op == "is_null":
            return actual is None
        if op == "is_empty":
            if actual is None:
                return True
            return bool(actual == "" or actual == [] or actual == {})
        msg = f"unknown operator: {op}"
        raise ValueError(msg)

    def _execute_action(self, action: Action, ctx: dict[str, Any]) -> None:
        handler = self._handlers.get(action.type)
        if handler is None:
            # 'call'/'calculate' and any unknown type are not in the default
            # registry — fail loudly so a typo or an unsupported action surfaces
            # instead of silently doing nothing (audit #215).  Callers can inject
            # custom handlers via the constructor to handle these types.
            msg = f"unsupported action type '{action.type}'; override _execute_action to handle it"
            raise NotImplementedError(msg)
        handler(action, ctx)

    @staticmethod
    def _read(path: str, ctx: dict[str, Any]) -> Any:
        cur: Any = ctx
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
            if cur is None:
                return None
        return cur

    @staticmethod
    def _write(path: str, value: Any, ctx: dict[str, Any]) -> None:
        parts = path.split(".")
        cur: Any = ctx
        for part in parts[:-1]:
            if isinstance(cur, dict):
                cur = cur.setdefault(part, {})
            else:
                if not hasattr(cur, part):
                    setattr(cur, part, {})
                cur = getattr(cur, part)
        last = parts[-1]
        if isinstance(cur, dict):
            cur[last] = value
        else:
            setattr(cur, last, value)


class EvaluationMode(Enum):
    """Controls how many rules in a :class:`RuleSet` are evaluated.

    ``ALL``
        Every enabled rule in the ruleset is evaluated in descending priority
        order (the current default).  All matching rules execute their actions
        against the **shared** context dict, so later rules see mutations made
        by earlier ones.

    ``FIRST_MATCH``
        Rules are evaluated in descending priority order and evaluation stops
        immediately after the first rule whose condition matched.  The returned
        result list contains every rule that was evaluated *up to and including*
        the first match; rules with lower priority are never evaluated and their
        actions never fire.  The shared-context semantics are identical to
        ``ALL`` for the subset of rules that *are* evaluated.
    """

    ALL = "all"
    FIRST_MATCH = "first_match"


class RuleSetEvaluator:
    """Evaluates an entire :class:`RuleSet` in priority order.

    Parameters
    ----------
    rule_evaluator:
        The per-rule evaluator to delegate to.  Defaults to a vanilla
        :class:`RuleEvaluator` (default action-handler registry).
    mode:
        :attr:`EvaluationMode.ALL` (default) evaluates every rule;
        :attr:`EvaluationMode.FIRST_MATCH` stops after the first match.
    """

    def __init__(
        self,
        rule_evaluator: RuleEvaluator | None = None,
        mode: EvaluationMode = EvaluationMode.ALL,
    ) -> None:
        self._evaluator = rule_evaluator or RuleEvaluator()
        self._mode = mode

    def evaluate(self, ruleset: RuleSet, ctx: dict[str, Any]) -> list[EvaluationResult]:
        results: list[EvaluationResult] = []
        for rule in ruleset.sorted_rules():
            result = self._evaluator.evaluate(rule, ctx)
            results.append(result)
            if self._mode is EvaluationMode.FIRST_MATCH and result.matched:
                break
        return results
