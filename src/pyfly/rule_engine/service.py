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
"""Rule-engine service facade.

:class:`RuleEngineService` is the primary application-facing entry point for
rule evaluation.  It satisfies :class:`~pyfly.rule_engine.ports.outbound.RuleEnginePort`
and composes a :class:`~pyfly.rule_engine.repository.RuleSetRepository` for
persistence with a :class:`~pyfly.rule_engine.evaluator.RuleSetEvaluator` for
evaluation, optionally emitting Prometheus-compatible metrics via a
:class:`~pyfly.observability.ports.MetricsRecorder`.

Metrics counters (all labelled by ``ruleset``)
-----------------------------------------------
``pyfly_rule_evaluations_total``
    Incremented once per :meth:`evaluate` / :meth:`evaluate_by_name` call.
``pyfly_rules_matched_total``
    Incremented for every result whose ``matched`` flag is ``True``.
``pyfly_rule_actions_fired_total``
    Incremented by the number of successfully-executed actions across all
    results.
``pyfly_rule_errors_total``
    Incremented for every result that carries a non-``None`` ``error`` field.
"""

from __future__ import annotations

from typing import Any

from pyfly.observability.ports import MetricsRecorder
from pyfly.rule_engine.dsl import RuleSet
from pyfly.rule_engine.evaluator import EvaluationResult, RuleSetEvaluator
from pyfly.rule_engine.repository import RuleSetRepository


class RuleSetNotFoundError(KeyError):
    """Raised by :meth:`RuleEngineService.evaluate_by_name` when no ruleset exists.

    Inherits from :exc:`KeyError` so callers that already handle ``KeyError``
    continue to work without changes.
    """

    def __init__(self, ruleset_id: str) -> None:
        self.ruleset_id = ruleset_id
        super().__init__(ruleset_id)

    def __str__(self) -> str:
        return f"RuleSet '{self.ruleset_id}' not found in repository"


class RuleEngineService:
    """Facade that wires a :class:`RuleSetRepository` and a :class:`RuleSetEvaluator`.

    Parameters
    ----------
    repository:
        The :class:`~pyfly.rule_engine.repository.RuleSetRepository` used by
        :meth:`evaluate_by_name`, :meth:`save_ruleset`, :meth:`get_ruleset`,
        and :meth:`list_rulesets`.
    evaluator:
        The :class:`RuleSetEvaluator` to use for all evaluation calls.
        Defaults to a vanilla :class:`RuleSetEvaluator` (``ALL`` mode, default
        action-handler registry) when omitted.
    metrics:
        An optional :class:`~pyfly.observability.ports.MetricsRecorder`.  When
        provided the service creates four counters on construction and
        increments them after every evaluation.  Omitting the recorder (or
        passing ``None``) is fully supported — the service operates as a
        no-op with regard to metrics.
    """

    def __init__(
        self,
        repository: RuleSetRepository,
        evaluator: RuleSetEvaluator | None = None,
        *,
        metrics: MetricsRecorder | None = None,
    ) -> None:
        self._repository = repository
        self._evaluator = evaluator or RuleSetEvaluator()

        if metrics is not None:
            self._evaluations = metrics.counter(
                "pyfly_rule_evaluations_total",
                "Total number of rule-set evaluation calls",
                labels=["ruleset"],
            )
            self._matched = metrics.counter(
                "pyfly_rules_matched_total",
                "Total number of rules that matched across all evaluations",
                labels=["ruleset"],
            )
            self._actions_fired = metrics.counter(
                "pyfly_rule_actions_fired_total",
                "Total number of actions successfully executed across all evaluations",
                labels=["ruleset"],
            )
            self._errors = metrics.counter(
                "pyfly_rule_errors_total",
                "Total number of rule results carrying an error across all evaluations",
                labels=["ruleset"],
            )
            self._metrics_enabled = True
        else:
            self._metrics_enabled = False

    # ------------------------------------------------------------------
    # Synchronous evaluation (satisfies RuleEnginePort)
    # ------------------------------------------------------------------

    def evaluate(self, ruleset: RuleSet, ctx: dict[str, Any]) -> list[EvaluationResult]:
        """Evaluate *ruleset* against *ctx* and return the list of results.

        This method satisfies :class:`~pyfly.rule_engine.ports.outbound.RuleEnginePort`.
        It is synchronous and can be called from any context (sync or async).
        """
        results = self._evaluator.evaluate(ruleset, ctx)
        self._record_metrics(ruleset.id, results)
        return results

    # ------------------------------------------------------------------
    # Async facade methods
    # ------------------------------------------------------------------

    async def evaluate_by_name(self, ruleset_id: str, ctx: dict[str, Any]) -> list[EvaluationResult]:
        """Load a ruleset by ID from the repository and evaluate it.

        Parameters
        ----------
        ruleset_id:
            The :attr:`~pyfly.rule_engine.dsl.RuleSet.id` to load.
        ctx:
            The mutable evaluation context dict.

        Raises
        ------
        RuleSetNotFoundError
            If the repository returns ``None`` for *ruleset_id*.
        """
        ruleset = await self._repository.get(ruleset_id)
        if ruleset is None:
            raise RuleSetNotFoundError(ruleset_id)
        return self.evaluate(ruleset, ctx)

    async def save_ruleset(self, rs: RuleSet) -> None:
        """Persist *rs* to the repository."""
        await self._repository.save(rs)

    async def get_ruleset(self, ruleset_id: str) -> RuleSet | None:
        """Return the ruleset with *ruleset_id*, or ``None`` if absent."""
        return await self._repository.get(ruleset_id)

    async def list_rulesets(self) -> list[RuleSet]:
        """Return all persisted rulesets."""
        return await self._repository.list()

    # ------------------------------------------------------------------
    # Metrics helpers
    # ------------------------------------------------------------------

    def _record_metrics(self, ruleset_id: str, results: list[EvaluationResult]) -> None:
        if not self._metrics_enabled:
            return
        self._evaluations.labels(ruleset=ruleset_id).inc()
        for result in results:
            if result.matched:
                self._matched.labels(ruleset=ruleset_id).inc()
            self._actions_fired.labels(ruleset=ruleset_id).inc(len(result.actions_executed))
            if result.error is not None:
                self._errors.labels(ruleset=ruleset_id).inc()
