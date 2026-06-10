# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""PyFly rule engine — YAML-based business rules with batch evaluation."""

from __future__ import annotations

from pyfly.rule_engine.builder import (
    RuleBuilder,
    RuleSetBuilder,
    _FieldBuilder,
    all_of,
    any_of,
    field,
    increment_action,
    log_action,
    not_,
    rule,
    ruleset,
    set_action,
)
from pyfly.rule_engine.dsl import (
    Action,
    Condition,
    Rule,
    RuleSet,
    RuleSetLoader,
)
from pyfly.rule_engine.evaluator import (
    EvaluationMode,
    EvaluationResult,
    RuleEvaluator,
    RuleSetEvaluator,
)
from pyfly.rule_engine.ports.outbound import ActionHandler, RuleEnginePort
from pyfly.rule_engine.repository import (
    InMemoryRuleSetRepository,
    RuleSetRepository,
)
from pyfly.rule_engine.service import RuleEngineService, RuleSetNotFoundError
from pyfly.rule_engine.validation import (
    RuleSetValidator,
    RuleValidationError,
    validate_ruleset,
)

__all__ = [
    "Action",
    "ActionHandler",
    "Condition",
    "EvaluationMode",
    "EvaluationResult",
    "InMemoryRuleSetRepository",
    "Rule",
    "RuleBuilder",
    "RuleEnginePort",
    "RuleEngineService",
    "RuleEvaluator",
    "RuleSet",
    "RuleSetBuilder",
    "RuleSetEvaluator",
    "RuleSetLoader",
    "RuleSetNotFoundError",
    "RuleSetRepository",
    "RuleSetValidator",
    "RuleValidationError",
    "_FieldBuilder",
    "all_of",
    "any_of",
    "field",
    "increment_action",
    "log_action",
    "not_",
    "rule",
    "ruleset",
    "set_action",
    "validate_ruleset",
]
