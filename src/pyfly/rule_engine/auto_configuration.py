# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the rule-engine module."""

from __future__ import annotations

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.rule_engine.evaluator import RuleEvaluator, RuleSetEvaluator
from pyfly.rule_engine.repository import InMemoryRuleSetRepository


@auto_configuration
@conditional_on_property("pyfly.rule-engine.enabled", having_value="true")
class RuleEngineAutoConfiguration:
    @bean
    def rule_set_repository(self) -> InMemoryRuleSetRepository:
        return InMemoryRuleSetRepository()

    @bean
    def rule_evaluator(self) -> RuleEvaluator:
        return RuleEvaluator()

    @bean
    def rule_set_evaluator(self, rule_evaluator: RuleEvaluator) -> RuleSetEvaluator:
        return RuleSetEvaluator(rule_evaluator=rule_evaluator)
