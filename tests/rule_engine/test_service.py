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
"""Tests for RuleEngineService facade (SP-13 Part B Item 3)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.rule_engine.dsl import Action, Condition, Rule, RuleSet
from pyfly.rule_engine.repository import InMemoryRuleSetRepository
from pyfly.rule_engine.service import RuleEngineService, RuleSetNotFoundError

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _simple_ruleset(ruleset_id: str = "test-rs") -> RuleSet:
    """A ruleset with one rule that always matches and increments a counter."""
    return RuleSet(
        id=ruleset_id,
        name="Test",
        rules=[
            Rule(
                id="r1",
                when=Condition(operator="eq", field="active", value=True),
                then=[Action(type="set", target="result", value="matched")],
                otherwise=[Action(type="set", target="result", value="not_matched")],
            )
        ],
    )


def _error_ruleset(ruleset_id: str = "err-rs") -> RuleSet:
    """A ruleset with one rule that always fires a bad (unregistered) action."""
    return RuleSet(
        id=ruleset_id,
        rules=[
            Rule(
                id="bad",
                then=[Action(type="nonexistent_action")],
            )
        ],
    )


class _FakeCounter:
    """Records all .labels(...).inc(amount) calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], float]] = []
        self._current_labels: dict[str, Any] = {}

    def labels(self, **kwargs: Any) -> _FakeCounter:
        self._current_labels = dict(kwargs)
        return self

    def inc(self, amount: float = 1) -> None:
        self.calls.append((dict(self._current_labels), amount))

    @property
    def total(self) -> float:
        return sum(v for _, v in self.calls)

    def total_for(self, **kwargs: Any) -> float:
        return sum(v for labels, v in self.calls if all(labels.get(k) == v2 for k, v2 in kwargs.items()))


class _FakeMetricsRecorder:
    """Minimal MetricsRecorder that tracks counter creations and increments."""

    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> _FakeCounter:
        if name not in self.counters:
            self.counters[name] = _FakeCounter()
        return self.counters[name]

    def histogram(self, name: str, description: str, labels: list[str] | None = None, buckets: Any = None) -> Any:
        return _FakeCounter()

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> Any:
        return _FakeCounter()


# ---------------------------------------------------------------------------
# Round-trip tests (repository + evaluation)
# ---------------------------------------------------------------------------


class TestEvaluateByName:
    @pytest.mark.asyncio
    async def test_round_trip_save_and_evaluate(self) -> None:
        """save_ruleset + evaluate_by_name evaluates the stored ruleset."""
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        rs = _simple_ruleset()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {"active": True}
        results = await svc.evaluate_by_name(rs.id, ctx)

        assert len(results) == 1
        assert results[0].matched is True
        assert ctx["result"] == "matched"

    @pytest.mark.asyncio
    async def test_evaluate_by_name_otherwise_branch(self) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        rs = _simple_ruleset()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {"active": False}
        results = await svc.evaluate_by_name(rs.id, ctx)

        assert results[0].matched is False
        assert ctx["result"] == "not_matched"

    @pytest.mark.asyncio
    async def test_evaluate_by_name_not_found_raises(self) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)

        with pytest.raises(RuleSetNotFoundError) as exc_info:
            await svc.evaluate_by_name("does-not-exist", {})

        assert "does-not-exist" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_evaluate_by_name_not_found_is_key_error(self) -> None:
        """RuleSetNotFoundError is a KeyError subclass for backward compat."""
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)

        with pytest.raises(KeyError):
            await svc.evaluate_by_name("missing", {})


# ---------------------------------------------------------------------------
# Synchronous evaluate() — satisfies RuleEnginePort
# ---------------------------------------------------------------------------


class TestSyncEvaluate:
    def test_sync_evaluate_returns_results(self) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        rs = _simple_ruleset()
        ctx: dict[str, Any] = {"active": True}
        results = svc.evaluate(rs, ctx)
        assert len(results) == 1
        assert results[0].matched is True


# ---------------------------------------------------------------------------
# Repository passthrough methods
# ---------------------------------------------------------------------------


class TestRepositoryPassthrough:
    @pytest.mark.asyncio
    async def test_get_ruleset_returns_saved(self) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        rs = _simple_ruleset("x")
        await svc.save_ruleset(rs)
        found = await svc.get_ruleset("x")
        assert found is rs

    @pytest.mark.asyncio
    async def test_get_ruleset_returns_none_when_absent(self) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        assert await svc.get_ruleset("nope") is None

    @pytest.mark.asyncio
    async def test_list_rulesets_returns_all(self) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        rs1 = _simple_ruleset("a")
        rs2 = _simple_ruleset("b")
        await svc.save_ruleset(rs1)
        await svc.save_ruleset(rs2)
        all_rs = await svc.list_rulesets()
        ids = {r.id for r in all_rs}
        assert ids == {"a", "b"}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    @pytest.fixture
    def recorder(self) -> _FakeMetricsRecorder:
        return _FakeMetricsRecorder()

    def test_counters_created_on_init(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        RuleEngineService(repository=repo, metrics=recorder)
        assert "pyfly_rule_evaluations_total" in recorder.counters
        assert "pyfly_rules_matched_total" in recorder.counters
        assert "pyfly_rule_actions_fired_total" in recorder.counters
        assert "pyfly_rule_errors_total" in recorder.counters

    def test_evaluations_counter_incremented(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _simple_ruleset("rs1")
        svc.evaluate(rs, {"active": True})

        evals = recorder.counters["pyfly_rule_evaluations_total"]
        assert evals.total == 1
        # label value preserved
        assert any(labels.get("ruleset") == "rs1" for labels, _ in evals.calls)

    def test_matched_counter_incremented(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _simple_ruleset("rs1")
        svc.evaluate(rs, {"active": True})

        matched = recorder.counters["pyfly_rules_matched_total"]
        assert matched.total == 1

    def test_no_match_does_not_increment_matched_counter(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _simple_ruleset("rs1")
        # active=False → rule condition is False → no match
        svc.evaluate(rs, {"active": False})

        matched = recorder.counters["pyfly_rules_matched_total"]
        assert matched.total == 0

    def test_actions_fired_counter_incremented(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _simple_ruleset("rs1")
        svc.evaluate(rs, {"active": True})

        actions = recorder.counters["pyfly_rule_actions_fired_total"]
        # one action executed (the "set" in the then branch)
        assert actions.total == 1

    def test_errors_counter_incremented_on_action_error(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _error_ruleset("err-rs")
        svc.evaluate(rs, {})

        errors = recorder.counters["pyfly_rule_errors_total"]
        assert errors.total == 1
        assert any(labels.get("ruleset") == "err-rs" for labels, _ in errors.calls)

    @pytest.mark.asyncio
    async def test_evaluate_by_name_increments_counters(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _simple_ruleset("rs2")
        await svc.save_ruleset(rs)
        await svc.evaluate_by_name("rs2", {"active": True})

        evals = recorder.counters["pyfly_rule_evaluations_total"]
        assert evals.total == 1
        assert any(labels.get("ruleset") == "rs2" for labels, _ in evals.calls)

    def test_no_metrics_recorder_no_error(self) -> None:
        """Service works fine without a metrics recorder."""
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        rs = _simple_ruleset()
        results = svc.evaluate(rs, {"active": True})
        assert len(results) == 1

    def test_multiple_evaluations_accumulate_counters(self, recorder: _FakeMetricsRecorder) -> None:
        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo, metrics=recorder)
        rs = _simple_ruleset("rs3")
        svc.evaluate(rs, {"active": True})
        svc.evaluate(rs, {"active": False})
        svc.evaluate(rs, {"active": True})

        evals = recorder.counters["pyfly_rule_evaluations_total"]
        assert evals.total == 3
        matched = recorder.counters["pyfly_rules_matched_total"]
        assert matched.total == 2
