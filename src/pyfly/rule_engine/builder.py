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
"""Fluent builder DSL for constructing :class:`~pyfly.rule_engine.dsl.Rule` objects.

Usage example::

    from pyfly.rule_engine.builder import field, all_of, any_of, not_
    from pyfly.rule_engine.builder import set_action, log_action, rule, ruleset

    my_rule = (
        rule("vip-discount")
        .describe("Grant discount to VIP customers with high spend")
        .priority(10)
        .when(
            all_of(
                field("customer.tier").eq("gold"),
                any_of(
                    field("order.total").ge(500),
                    field("customer.is_vip").eq(True),
                ),
            )
        )
        .then(
            set_action("order.discount_pct", 20),
            log_action("VIP discount applied"),
        )
        .otherwise(set_action("order.discount_pct", 0))
        .build()
    )
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pyfly.rule_engine.dsl import Action, Condition, Rule, RuleSet

# ---------------------------------------------------------------------------
# Condition helpers
# ---------------------------------------------------------------------------


class _FieldBuilder:
    """Intermediate builder returned by :func:`field`; produces a :class:`Condition`."""

    __slots__ = ("_path",)

    def __init__(self, path: str) -> None:
        self._path = path

    def _leaf(self, op: str, value: Any = None) -> Condition:
        return Condition(operator=op, field=self._path, value=value)

    def eq(self, value: Any) -> Condition:  # noqa: ANN401
        """Equal."""
        return self._leaf("eq", value)

    def ne(self, value: Any) -> Condition:  # noqa: ANN401
        """Not equal."""
        return self._leaf("ne", value)

    def gt(self, value: Any) -> Condition:  # noqa: ANN401
        """Greater-than."""
        return self._leaf("gt", value)

    def ge(self, value: Any) -> Condition:  # noqa: ANN401
        """Greater-than-or-equal."""
        return self._leaf("ge", value)

    def lt(self, value: Any) -> Condition:  # noqa: ANN401
        """Less-than."""
        return self._leaf("lt", value)

    def le(self, value: Any) -> Condition:  # noqa: ANN401
        """Less-than-or-equal."""
        return self._leaf("le", value)

    def in_(self, seq: Sequence[Any]) -> Condition:
        """Membership: ``actual in seq``."""
        return self._leaf("in", list(seq))

    def not_in(self, seq: Sequence[Any]) -> Condition:
        """Non-membership: ``actual not in seq``."""
        return self._leaf("not_in", list(seq))

    def regex(self, pattern: str) -> Condition:
        """Regex search against the field value."""
        return self._leaf("regex", pattern)

    def between(self, lo: Any, hi: Any) -> Condition:  # noqa: ANN401
        """Range check: ``lo <= actual <= hi``."""
        return self._leaf("between", [lo, hi])

    def contains(self, value: Any) -> Condition:  # noqa: ANN401
        """Collection/string containment."""
        return self._leaf("contains", value)

    def not_contains(self, value: Any) -> Condition:  # noqa: ANN401
        """Inverse of :meth:`contains`."""
        return self._leaf("not_contains", value)

    def starts_with(self, prefix: str) -> Condition:
        """String prefix test."""
        return self._leaf("starts_with", prefix)

    def ends_with(self, suffix: str) -> Condition:
        """String suffix test."""
        return self._leaf("ends_with", suffix)

    def exists(self) -> Condition:
        """True if the field is present and not None."""
        return self._leaf("exists")

    def is_null(self) -> Condition:
        """True if the field is absent or None."""
        return self._leaf("is_null")

    def is_empty(self) -> Condition:
        """True if the field is None, '', [], or {}."""
        return self._leaf("is_empty")


def field(path: str) -> _FieldBuilder:
    """Return a :class:`_FieldBuilder` for *path* (dot-notation context access).

    Example::

        field("order.total").ge(100)
    """
    return _FieldBuilder(path)


def all_of(*conditions: Condition) -> Condition:
    """Return an ``and`` compound condition (all children must be true)."""
    return Condition(operator="and", children=list(conditions))


def any_of(*conditions: Condition) -> Condition:
    """Return an ``or`` compound condition (at least one child must be true)."""
    return Condition(operator="or", children=list(conditions))


def not_(condition: Condition) -> Condition:
    """Return a ``not`` compound condition (negates exactly one child)."""
    return Condition(operator="not", children=[condition])


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------


def set_action(target: str, value: Any) -> Action:  # noqa: ANN401
    """Return a ``set`` action that writes *value* to *target*."""
    return Action(type="set", target=target, value=value)


def increment_action(target: str, by: int | float = 1) -> Action:
    """Return an ``increment`` action that adds *by* (default 1) to *target*."""
    return Action(type="increment", target=target, value=by)


def log_action(message: str) -> Action:
    """Return a ``log`` action that logs *message*."""
    return Action(type="log", value=message)


# ---------------------------------------------------------------------------
# Rule builder
# ---------------------------------------------------------------------------


class RuleBuilder:
    """Fluent builder for a single :class:`~pyfly.rule_engine.dsl.Rule`.

    Create via :func:`rule`::

        rule("my-id").describe("…").priority(5).when(cond).then(action).build()
    """

    def __init__(self, rule_id: str) -> None:
        self._id = rule_id
        self._description: str = ""
        self._priority: int = 0
        self._enabled: bool = True
        self._when: Condition | None = None
        self._then: list[Action] = []
        self._otherwise: list[Action] = []

    def describe(self, text: str) -> RuleBuilder:
        """Set the human-readable description."""
        self._description = text
        return self

    def priority(self, n: int) -> RuleBuilder:
        """Set evaluation priority (higher = evaluated first)."""
        self._priority = n
        return self

    def enabled(self, flag: bool) -> RuleBuilder:
        """Enable or disable the rule (disabled rules are skipped)."""
        self._enabled = flag
        return self

    def when(self, condition: Condition) -> RuleBuilder:
        """Set the condition that must be true for *then* actions to run."""
        self._when = condition
        return self

    def then(self, *actions: Action) -> RuleBuilder:
        """Append actions to execute when the condition matches."""
        self._then.extend(actions)
        return self

    def otherwise(self, *actions: Action) -> RuleBuilder:
        """Append actions to execute when the condition does not match."""
        self._otherwise.extend(actions)
        return self

    def build(self) -> Rule:
        """Return the constructed :class:`Rule`."""
        return Rule(
            id=self._id,
            description=self._description,
            when=self._when,
            then=list(self._then),
            otherwise=list(self._otherwise),
            priority=self._priority,
            enabled=self._enabled,
        )


def rule(rule_id: str) -> RuleBuilder:
    """Return a :class:`RuleBuilder` for *rule_id*."""
    return RuleBuilder(rule_id)


# ---------------------------------------------------------------------------
# RuleSet builder
# ---------------------------------------------------------------------------


class RuleSetBuilder:
    """Fluent builder for a :class:`~pyfly.rule_engine.dsl.RuleSet`.

    Create via :func:`ruleset`::

        ruleset("my-rs", name="My Rules").add(my_rule).build()
    """

    def __init__(self, ruleset_id: str, name: str = "", version: int = 1) -> None:
        self._id = ruleset_id
        self._name = name
        self._version = version
        self._rules: list[Rule] = []

    def add(self, *rules: Rule) -> RuleSetBuilder:
        """Append one or more rules to the rule set."""
        self._rules.extend(rules)
        return self

    def build(self) -> RuleSet:
        """Return the constructed :class:`RuleSet`."""
        return RuleSet(
            id=self._id,
            name=self._name,
            version=self._version,
            rules=list(self._rules),
        )


def ruleset(ruleset_id: str, name: str = "", version: int = 1) -> RuleSetBuilder:
    """Return a :class:`RuleSetBuilder` for *ruleset_id*."""
    return RuleSetBuilder(ruleset_id, name=name, version=version)
