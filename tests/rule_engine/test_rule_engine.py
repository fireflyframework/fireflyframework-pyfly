# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the rule engine."""

from __future__ import annotations

import pytest

from pyfly.rule_engine.dsl import RuleSet, RuleSetLoader
from pyfly.rule_engine.evaluator import RuleSetEvaluator
from pyfly.rule_engine.repository import InMemoryRuleSetRepository


def _yaml_ruleset() -> str:
    return """
id: order-rules
name: Order processing
rules:
  - id: high-value
    priority: 10
    when:
      op: ge
      field: order.amount
      value: 1000
    then:
      - type: set
        target: flags.high_value
        value: true
  - id: cheap
    priority: 5
    when:
      op: lt
      field: order.amount
      value: 100
    then:
      - type: set
        target: flags.cheap
        value: true
"""


def test_load_yaml() -> None:
    ruleset = RuleSetLoader.from_yaml(_yaml_ruleset())
    assert ruleset.id == "order-rules"
    assert len(ruleset.rules) == 2


def test_evaluate_high_value() -> None:
    ruleset = RuleSetLoader.from_yaml(_yaml_ruleset())
    evaluator = RuleSetEvaluator()
    ctx = {"order": {"amount": 5000}, "flags": {}}
    results = evaluator.evaluate(ruleset, ctx)
    assert any(r.matched for r in results if r.rule_id == "high-value")
    assert ctx["flags"]["high_value"] is True


def test_evaluate_cheap_path() -> None:
    ruleset = RuleSetLoader.from_yaml(_yaml_ruleset())
    evaluator = RuleSetEvaluator()
    ctx = {"order": {"amount": 50}, "flags": {}}
    evaluator.evaluate(ruleset, ctx)
    assert ctx["flags"]["cheap"] is True


@pytest.mark.asyncio
async def test_repository_round_trip() -> None:
    repo = InMemoryRuleSetRepository()
    rs = RuleSet(id="x", name="x")
    await repo.save(rs)
    fetched = await repo.get("x")
    assert fetched is not None
    assert (await repo.list())[0].id == "x"
    assert await repo.delete("x")


def test_logical_operators() -> None:
    yaml_text = """
id: combo
rules:
  - id: r
    when:
      op: and
      conditions:
        - { op: ge, field: x, value: 5 }
        - { op: le, field: x, value: 10 }
    then:
      - { type: set, target: ok, value: true }
"""
    ruleset = RuleSetLoader.from_yaml(yaml_text)
    ev = RuleSetEvaluator()
    ctx = {"x": 7}
    ev.evaluate(ruleset, ctx)
    assert ctx.get("ok") is True
