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
"""Rule-engine outbound port protocols.

:class:`ActionHandler` is the pluggable action-execution SPI — implement it to
add custom action types (``call``, ``calculate``, HTTP invocations, etc.) without
subclassing :class:`~pyfly.rule_engine.evaluator.RuleEvaluator`.

:class:`RuleEnginePort` is the inbound-facing port that application code
depends on; :class:`~pyfly.rule_engine.service.RuleEngineService` satisfies it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pyfly.rule_engine.dsl import Action, RuleSet
from pyfly.rule_engine.evaluator import EvaluationResult


@runtime_checkable
class ActionHandler(Protocol):
    """SPI for executing a single rule action.

    A handler is any *callable* taking ``(action, ctx)`` — a plain function, a
    lambda, or an object with ``__call__`` — so it matches the registry shape
    used by :class:`~pyfly.rule_engine.evaluator.RuleEvaluator` exactly. Each
    handler is registered under its action *type* string (e.g. ``"call"``,
    ``"http"``) in that evaluator's action-handler registry. The handler
    receives the full :class:`Action` (so it can inspect ``target``, ``value``,
    ``expression``, ``arguments``) and the mutable evaluation *context* dict.
    """

    def __call__(self, action: Action, ctx: dict[str, Any]) -> None: ...


@runtime_checkable
class RuleEnginePort(Protocol):
    """Primary port for rule-set evaluation.

    Application code that drives rule evaluation depends on this abstraction
    rather than on :class:`~pyfly.rule_engine.service.RuleEngineService`
    directly, enabling test-doubles and alternate implementations.
    """

    def evaluate(self, ruleset: RuleSet, ctx: dict[str, Any]) -> list[EvaluationResult]: ...
