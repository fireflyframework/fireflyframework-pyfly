# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Auto-configuration for the rule-engine module."""

from __future__ import annotations

from typing import Any

from pyfly.container.bean import bean
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core.config import Config
from pyfly.rule_engine.evaluator import EvaluationMode, RuleEvaluator, RuleSetEvaluator
from pyfly.rule_engine.repository import InMemoryRuleSetRepository
from pyfly.rule_engine.service import RuleEngineService


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
    def rule_set_evaluator(self, rule_evaluator: RuleEvaluator, config: Config) -> RuleSetEvaluator:
        mode_str: str = config.get("pyfly.rule-engine.mode", "all")
        mode = EvaluationMode.FIRST_MATCH if mode_str == "first-match" else EvaluationMode.ALL
        return RuleSetEvaluator(rule_evaluator=rule_evaluator, mode=mode)

    @bean
    def rule_engine_service(
        self,
        rule_set_repository: InMemoryRuleSetRepository,
        rule_set_evaluator: RuleSetEvaluator,
        metrics: Any | None = None,
    ) -> RuleEngineService:
        return RuleEngineService(
            repository=rule_set_repository,
            evaluator=rule_set_evaluator,
            metrics=metrics,
        )
