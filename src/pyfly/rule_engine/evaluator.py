# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Pure-python rule evaluator — runs over an evaluation context dict."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pyfly.rule_engine.dsl import Action, Condition, Rule, RuleSet


@dataclass
class EvaluationResult:
    rule_id: str
    matched: bool
    actions_executed: list[Action] = field(default_factory=list)
    error: str | None = None


class RuleEvaluator:
    """Single-rule evaluator."""

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
        if action.type == "set":
            if action.target is None:
                msg = "set action missing 'target'"
                raise ValueError(msg)
            self._write(action.target, action.value, ctx)
        elif action.type == "increment":
            if action.target is None:
                msg = "increment action missing 'target'"
                raise ValueError(msg)
            current = self._read(action.target, ctx) or 0
            self._write(action.target, current + (action.value or 1), ctx)
        elif action.type == "log":
            import logging

            logging.getLogger(__name__).info("rule action: %s", action.value or action.target)
        else:
            # 'call'/'calculate' and any unknown type are not implemented by the
            # default evaluator — fail loudly so a typo or an unsupported action
            # surfaces instead of silently doing nothing (audit #215). Real
            # services override _execute_action to plug in HTTP calls, etc.
            msg = f"unsupported action type '{action.type}'; override _execute_action to handle it"
            raise NotImplementedError(msg)

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


class RuleSetEvaluator:
    """Evaluates an entire :class:`RuleSet` in priority order."""

    def __init__(self, rule_evaluator: RuleEvaluator | None = None) -> None:
        self._evaluator = rule_evaluator or RuleEvaluator()

    def evaluate(self, ruleset: RuleSet, ctx: dict[str, Any]) -> list[EvaluationResult]:
        return [self._evaluator.evaluate(rule, ctx) for rule in ruleset.sorted_rules()]
