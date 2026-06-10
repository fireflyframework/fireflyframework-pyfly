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
"""End-to-end integration test for the rule engine (SP-13 Part C Item 1).

Exercises the full stack together — fluent builder, YAML DSL, loader,
validator, InMemoryRuleSetRepository, RuleEngineService, custom action
handlers, EvaluationMode.ALL / FIRST_MATCH, and MetricsRecorder — without
any external infrastructure (no Docker, no HTTP, no Prometheus server).

Scenario: *order-processing* ruleset
-------------------------------------
Three rules evaluated against order-processing contexts:

``high-value``  (priority 20)
    Condition: ``order.amount >= 5000`` (between 5000–999999)
    then:  set ``flags.high_value=True``  + increment ``score`` by 10
    otherwise: set ``flags.high_value=False``

``region-blocked`` (priority 15)
    Condition: ``order.region`` in ["RU", "KP", "IR"]
    then:  set ``flags.blocked=True``
    otherwise: set ``flags.blocked=False``

``fraud-pattern``  (priority 10)
    Condition: ``order.email`` regex ``.*@temp.*\\..*``
    then:  set ``flags.fraud_suspected=True``  + call ``audit`` handler
    otherwise: set ``flags.fraud_suspected=False``

Both a fluent-builder ruleset and a YAML-loaded ruleset are constructed and
asserted equivalent before any evaluation.
"""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from pyfly.rule_engine.builder import (
    all_of,
    field,
    increment_action,
    rule,
    ruleset,
    set_action,
)
from pyfly.rule_engine.dsl import Action, RuleSetLoader
from pyfly.rule_engine.evaluator import EvaluationMode, RuleEvaluator, RuleSetEvaluator
from pyfly.rule_engine.repository import InMemoryRuleSetRepository
from pyfly.rule_engine.service import RuleEngineService, RuleSetNotFoundError
from pyfly.rule_engine.validation import validate_ruleset

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeCounter:
    """Minimal counter double that records .labels(**kw).inc(amount) calls."""

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


class _FakeMetricsRecorder:
    """Minimal MetricsRecorder double that captures counter creations and increments."""

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
# Audit handler (custom "call"-type action)
# ---------------------------------------------------------------------------


class _AuditLog:
    """Captures 'call'-type audit side-effects during evaluation."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def handle(self, action: Action, ctx: dict[str, Any]) -> None:
        """Record the action arguments and the current order id as an audit event."""
        self.events.append(
            {
                "event": action.arguments.get("event", action.target),
                "order_id": ctx.get("order", {}).get("id"),
            }
        )


# ---------------------------------------------------------------------------
# YAML document
# ---------------------------------------------------------------------------

_ORDER_PROCESSING_YAML = textwrap.dedent("""\
    id: order-processing
    name: Order Processing Rules
    version: 1
    rules:
      - id: high-value
        description: Flag high-value orders and boost risk score
        priority: 20
        when:
          op: between
          field: order.amount
          value: [5000, 999999]
        then:
          - { type: set, target: flags.high_value, value: true }
          - { type: increment, target: score, value: 10 }
        otherwise:
          - { type: set, target: flags.high_value, value: false }

      - id: region-blocked
        description: Block orders from sanctioned regions
        priority: 15
        when:
          op: in
          field: order.region
          value: ["RU", "KP", "IR"]
        then:
          - { type: set, target: flags.blocked, value: true }
        otherwise:
          - { type: set, target: flags.blocked, value: false }

      - id: fraud-pattern
        description: Detect disposable-email fraud pattern
        priority: 10
        when:
          op: regex
          field: order.email
          value: ".*@temp.*\\\\..*"
        then:
          - { type: set, target: flags.fraud_suspected, value: true }
          - type: call
            target: fraud-audit
            arguments: { event: fraud_pattern_matched }
        otherwise:
          - { type: set, target: flags.fraud_suspected, value: false }
""")


# ---------------------------------------------------------------------------
# Builder-based ruleset factory
# ---------------------------------------------------------------------------


def _build_ruleset_via_builder() -> Any:
    """Build the order-processing ruleset using the fluent builder API."""
    audit_action = Action(
        type="call",
        target="fraud-audit",
        arguments={"event": "fraud_pattern_matched"},
    )

    high_value_rule = (
        rule("high-value")
        .describe("Flag high-value orders and boost risk score")
        .priority(20)
        .when(field("order.amount").between(5000, 999999))
        .then(set_action("flags.high_value", True), increment_action("score", 10))
        .otherwise(set_action("flags.high_value", False))
        .build()
    )

    region_blocked_rule = (
        rule("region-blocked")
        .describe("Block orders from sanctioned regions")
        .priority(15)
        .when(field("order.region").in_(["RU", "KP", "IR"]))
        .then(set_action("flags.blocked", True))
        .otherwise(set_action("flags.blocked", False))
        .build()
    )

    fraud_rule = (
        rule("fraud-pattern")
        .describe("Detect disposable-email fraud pattern")
        .priority(10)
        .when(field("order.email").regex(r".*@temp.*\..*"))
        .then(set_action("flags.fraud_suspected", True), audit_action)
        .otherwise(set_action("flags.fraud_suspected", False))
        .build()
    )

    return (
        ruleset("order-processing", name="Order Processing Rules", version=1)
        .add(high_value_rule, region_blocked_rule, fraud_rule)
        .build()
    )


# ---------------------------------------------------------------------------
# Helper: fresh service wired to InMemoryRuleSetRepository
# ---------------------------------------------------------------------------


def _make_service(
    mode: EvaluationMode = EvaluationMode.ALL,
    audit_log: _AuditLog | None = None,
    metrics: _FakeMetricsRecorder | None = None,
) -> RuleEngineService:
    """Create a fully-wired RuleEngineService for tests."""
    extra_handlers: dict[str, Any] = {}
    if audit_log is not None:
        extra_handlers["call"] = audit_log.handle

    evaluator = RuleEvaluator(action_handlers=extra_handlers if extra_handlers else None)
    set_evaluator = RuleSetEvaluator(rule_evaluator=evaluator, mode=mode)
    repo = InMemoryRuleSetRepository()
    return RuleEngineService(repository=repo, evaluator=set_evaluator, metrics=metrics)


# ===========================================================================
# Test class 1 — Builder / YAML equivalence + validation
# ===========================================================================


class TestBuilderYamlEquivalence:
    """The builder and the YAML loader produce equivalent RuleSets."""

    def test_same_id_name_version(self) -> None:
        builder_rs = _build_ruleset_via_builder()
        yaml_rs = RuleSetLoader.from_yaml(_ORDER_PROCESSING_YAML)

        assert builder_rs.id == yaml_rs.id
        assert builder_rs.name == yaml_rs.name
        assert builder_rs.version == yaml_rs.version

    def test_same_rule_count_and_ids(self) -> None:
        builder_rs = _build_ruleset_via_builder()
        yaml_rs = RuleSetLoader.from_yaml(_ORDER_PROCESSING_YAML)

        builder_ids = [r.id for r in builder_rs.rules]
        yaml_ids = [r.id for r in yaml_rs.rules]
        assert builder_ids == yaml_ids

    def test_same_priorities(self) -> None:
        builder_rs = _build_ruleset_via_builder()
        yaml_rs = RuleSetLoader.from_yaml(_ORDER_PROCESSING_YAML)

        for br, yr in zip(builder_rs.rules, yaml_rs.rules, strict=True):
            assert br.priority == yr.priority, f"priority mismatch for rule '{br.id}'"

    def test_same_condition_operators_and_fields(self) -> None:
        builder_rs = _build_ruleset_via_builder()
        yaml_rs = RuleSetLoader.from_yaml(_ORDER_PROCESSING_YAML)

        for br, yr in zip(builder_rs.rules, yaml_rs.rules, strict=True):
            assert br.when is not None and yr.when is not None
            assert br.when.operator == yr.when.operator, f"operator mismatch for rule '{br.id}'"
            assert br.when.field == yr.when.field, f"field mismatch for rule '{br.id}'"

    def test_same_then_action_types(self) -> None:
        builder_rs = _build_ruleset_via_builder()
        yaml_rs = RuleSetLoader.from_yaml(_ORDER_PROCESSING_YAML)

        for br, yr in zip(builder_rs.rules, yaml_rs.rules, strict=True):
            b_types = [a.type for a in br.then]
            y_types = [a.type for a in yr.then]
            assert b_types == y_types, f"then-action types mismatch for rule '{br.id}'"

    def test_validation_yields_no_issues(self) -> None:
        """Both representations pass validation cleanly."""
        builder_rs = _build_ruleset_via_builder()
        yaml_rs = RuleSetLoader.from_yaml(_ORDER_PROCESSING_YAML)

        assert validate_ruleset(builder_rs) == []
        assert validate_ruleset(yaml_rs) == []


# ===========================================================================
# Test class 2 — EvaluationMode.ALL full-stack scenario
# ===========================================================================


class TestAllModeFullStack:
    """save_ruleset + evaluate_by_name with ALL mode across varied order contexts."""

    @pytest.mark.asyncio
    async def test_high_value_order_sets_flag_and_increments_score(self) -> None:
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.ALL, audit_log=audit_log)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-001", "amount": 9000, "region": "US", "email": "alice@example.com"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        # Three rules evaluated in ALL mode
        assert len(results) == 3

        # high-value rule matched — flag + score
        assert ctx["flags"]["high_value"] is True
        assert ctx["score"] == 10

        # region not blocked
        assert ctx["flags"]["blocked"] is False

        # email not fraud
        assert ctx["flags"]["fraud_suspected"] is False

        # audit handler did NOT fire (no fraud match)
        assert len(audit_log.events) == 0

    @pytest.mark.asyncio
    async def test_low_value_order_does_not_set_high_value_flag(self) -> None:
        svc = _make_service(mode=EvaluationMode.ALL)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-002", "amount": 100, "region": "US", "email": "bob@example.com"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx)

        assert ctx["flags"]["high_value"] is False
        assert ctx["score"] == 0

    @pytest.mark.asyncio
    async def test_blocked_region_sets_flag(self) -> None:
        svc = _make_service(mode=EvaluationMode.ALL)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-003", "amount": 200, "region": "KP", "email": "x@example.com"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx)

        assert ctx["flags"]["blocked"] is True

    @pytest.mark.asyncio
    async def test_fraud_email_fires_custom_audit_handler(self) -> None:
        """A ``call``-type action triggers the injected audit handler."""
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.ALL, audit_log=audit_log)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-004", "amount": 50, "region": "US", "email": "hacker@temp.mail.org"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        # fraud-pattern rule should have matched
        fraud_result = next(r for r in results if r.rule_id == "fraud-pattern")
        assert fraud_result.matched is True

        # audit handler recorded one event
        assert len(audit_log.events) == 1
        assert audit_log.events[0]["event"] == "fraud_pattern_matched"
        assert audit_log.events[0]["order_id"] == "ORD-004"

        # context flag set
        assert ctx["flags"]["fraud_suspected"] is True

    @pytest.mark.asyncio
    async def test_all_three_rules_match_simultaneously(self) -> None:
        """A single order can match all three rules at once (ALL mode)."""
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.ALL, audit_log=audit_log)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-005", "amount": 7500, "region": "RU", "email": "spy@temp.io.ru"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        assert len(results) == 3
        assert all(r.matched for r in results)

        assert ctx["flags"]["high_value"] is True
        assert ctx["score"] == 10
        assert ctx["flags"]["blocked"] is True
        assert ctx["flags"]["fraud_suspected"] is True
        assert len(audit_log.events) == 1

    @pytest.mark.asyncio
    async def test_matched_rules_in_results(self) -> None:
        """result.matched reflects per-rule match status in ALL mode."""
        svc = _make_service(mode=EvaluationMode.ALL)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        # amount triggers high-value; region is not blocked; email is clean
        ctx: dict[str, Any] = {
            "order": {"id": "ORD-006", "amount": 6000, "region": "DE", "email": "clean@company.com"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        by_id = {r.rule_id: r for r in results}
        assert by_id["high-value"].matched is True
        assert by_id["region-blocked"].matched is False
        assert by_id["fraud-pattern"].matched is False

    @pytest.mark.asyncio
    async def test_actions_executed_listed_in_result(self) -> None:
        """EvaluationResult.actions_executed lists only the actually-run actions."""
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.ALL, audit_log=audit_log)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-007", "amount": 8000, "region": "US", "email": "ok@example.com"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        high_result = next(r for r in results if r.rule_id == "high-value")
        # then-branch has set + increment → 2 actions executed
        assert len(high_result.actions_executed) == 2
        assert [a.type for a in high_result.actions_executed] == ["set", "increment"]


# ===========================================================================
# Test class 3 — EvaluationMode.FIRST_MATCH full-stack scenario
# ===========================================================================


class TestFirstMatchModeFullStack:
    """FIRST_MATCH stops after the highest-priority matching rule fires."""

    @pytest.mark.asyncio
    async def test_first_match_stops_at_high_value_rule(self) -> None:
        """When high-value matches, only that rule's actions fire."""
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.FIRST_MATCH, audit_log=audit_log)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ORD-010", "amount": 9999, "region": "RU", "email": "fraud@temp.io"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        # Only one result — high-value (priority 20) matched first
        assert len(results) == 1
        assert results[0].rule_id == "high-value"
        assert results[0].matched is True

        # high-value actions ran
        assert ctx["flags"]["high_value"] is True
        assert ctx["score"] == 10

        # region-blocked and fraud-pattern actions did NOT run
        assert "blocked" not in ctx["flags"]
        assert "fraud_suspected" not in ctx["flags"]

        # audit handler did NOT fire
        assert len(audit_log.events) == 0

    @pytest.mark.asyncio
    async def test_first_match_skips_to_second_rule_when_first_misses(self) -> None:
        """If high-value does not match, evaluation continues until a match is found."""
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.FIRST_MATCH, audit_log=audit_log)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        # amount is low (< 5000) → high-value does NOT match
        # region is blocked → region-blocked MATCHES → stops
        ctx: dict[str, Any] = {
            "order": {"id": "ORD-011", "amount": 100, "region": "IR", "email": "ok@example.com"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        # Two results: high-value (no match), region-blocked (match → stop)
        assert len(results) == 2
        assert results[0].rule_id == "high-value"
        assert results[0].matched is False
        assert results[1].rule_id == "region-blocked"
        assert results[1].matched is True

        # Only region-blocked's then-branch ran
        assert ctx["flags"]["blocked"] is True
        # fraud-pattern was never evaluated
        assert "fraud_suspected" not in ctx["flags"]
        assert len(audit_log.events) == 0

    @pytest.mark.asyncio
    async def test_first_match_all_rules_evaluated_when_no_match(self) -> None:
        """When no rule matches in FIRST_MATCH all rules are still evaluated."""
        svc = _make_service(mode=EvaluationMode.FIRST_MATCH)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        # amount < 5000, region not blocked, email clean → 0 matches
        ctx: dict[str, Any] = {
            "order": {"id": "ORD-012", "amount": 50, "region": "US", "email": "ok@example.com"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        # All three rules evaluated, none matched
        assert len(results) == 3
        assert not any(r.matched for r in results)


# ===========================================================================
# Test class 4 — Metrics counter integration
# ===========================================================================


class TestMetricsIntegration:
    """Counters are incremented correctly through the service → evaluator pipeline."""

    @pytest.mark.asyncio
    async def test_evaluations_counter_incremented_per_call(self) -> None:
        recorder = _FakeMetricsRecorder()
        svc = _make_service(mode=EvaluationMode.ALL, metrics=recorder)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx1: dict[str, Any] = {
            "order": {"id": "M-001", "amount": 200, "region": "US", "email": "a@b.com"},
            "score": 0,
            "flags": {},
        }
        ctx2: dict[str, Any] = {
            "order": {"id": "M-002", "amount": 200, "region": "US", "email": "a@b.com"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx1)
        await svc.evaluate_by_name("order-processing", ctx2)

        evals = recorder.counters["pyfly_rule_evaluations_total"]
        assert evals.total == 2
        assert all(labels.get("ruleset") == "order-processing" for labels, _ in evals.calls)

    @pytest.mark.asyncio
    async def test_matched_counter_reflects_matching_rules(self) -> None:
        recorder = _FakeMetricsRecorder()
        svc = _make_service(mode=EvaluationMode.ALL, metrics=recorder)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        # Only high-value matches
        ctx: dict[str, Any] = {
            "order": {"id": "M-003", "amount": 6000, "region": "DE", "email": "clean@co.com"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx)

        matched = recorder.counters["pyfly_rules_matched_total"]
        assert matched.total == 1

    @pytest.mark.asyncio
    async def test_actions_fired_counter_counts_all_executed_actions(self) -> None:
        recorder = _FakeMetricsRecorder()
        audit_log = _AuditLog()
        svc = _make_service(mode=EvaluationMode.ALL, audit_log=audit_log, metrics=recorder)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        # high-value matches (2 actions: set + increment)
        # region not blocked (1 otherwise action: set)
        # fraud not suspected (1 otherwise action: set)
        # total executed actions: 2 + 1 + 1 = 4
        ctx: dict[str, Any] = {
            "order": {"id": "M-004", "amount": 7000, "region": "US", "email": "safe@company.com"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx)

        actions_fired = recorder.counters["pyfly_rule_actions_fired_total"]
        assert actions_fired.total == 4

    @pytest.mark.asyncio
    async def test_errors_counter_incremented_on_unregistered_action(self) -> None:
        """When a 'call' action has no handler, the error counter increments."""
        recorder = _FakeMetricsRecorder()
        # No audit_log → 'call' handler NOT registered → NotImplementedError
        svc = _make_service(mode=EvaluationMode.ALL, metrics=recorder)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "M-005", "amount": 50, "region": "US", "email": "x@temp.spam.io"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx)

        errors = recorder.counters["pyfly_rule_errors_total"]
        # fraud-pattern matched → 'call' action fails → 1 error
        assert errors.total == 1


# ===========================================================================
# Test class 5 — Repository round-trip and error handling
# ===========================================================================


class TestRepositoryAndErrorHandling:
    @pytest.mark.asyncio
    async def test_evaluate_by_name_not_found_raises(self) -> None:
        """evaluate_by_name raises RuleSetNotFoundError when the ID is unknown."""
        svc = _make_service()
        with pytest.raises(RuleSetNotFoundError) as exc_info:
            await svc.evaluate_by_name("does-not-exist", {})
        assert "does-not-exist" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_list_rulesets_returns_saved(self) -> None:
        svc = _make_service()
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)
        all_rs = await svc.list_rulesets()
        assert len(all_rs) == 1
        assert all_rs[0].id == "order-processing"

    @pytest.mark.asyncio
    async def test_get_ruleset_round_trip(self) -> None:
        svc = _make_service()
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)
        retrieved = await svc.get_ruleset("order-processing")
        assert retrieved is not None
        assert retrieved.id == "order-processing"
        assert len(retrieved.rules) == 3

    @pytest.mark.asyncio
    async def test_shared_context_mutations_in_all_mode(self) -> None:
        """In ALL mode earlier rules (higher priority) mutate ctx visible to later ones."""
        svc = _make_service(mode=EvaluationMode.ALL)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        # high-value matches first (p=20), sets flags.high_value=True and score=10
        # later rules can observe those mutations
        ctx: dict[str, Any] = {
            "order": {"id": "CTX-001", "amount": 6000, "region": "US", "email": "ok@example.com"},
            "score": 0,
            "flags": {},
        }
        await svc.evaluate_by_name("order-processing", ctx)

        # Verify mutations accumulated across all rules
        assert ctx["score"] == 10  # set by high-value
        assert ctx["flags"]["high_value"] is True
        assert ctx["flags"]["blocked"] is False
        assert ctx["flags"]["fraud_suspected"] is False

    @pytest.mark.asyncio
    async def test_action_isolation_bad_call_does_not_prevent_sibling_actions(self) -> None:
        """An unregistered 'call' action fails in isolation; sibling 'set' still runs."""
        # No custom 'call' handler registered
        svc = _make_service(mode=EvaluationMode.ALL)
        rs = _build_ruleset_via_builder()
        await svc.save_ruleset(rs)

        ctx: dict[str, Any] = {
            "order": {"id": "ISO-001", "amount": 50, "region": "US", "email": "hacker@temp.mail.net"},
            "score": 0,
            "flags": {},
        }
        results = await svc.evaluate_by_name("order-processing", ctx)

        fraud_result = next(r for r in results if r.rule_id == "fraud-pattern")
        # The 'set' action executed successfully
        assert ctx["flags"]["fraud_suspected"] is True
        # The 'call' action failed, but error is isolated to the result
        assert fraud_result.error is not None
        assert "call" in fraud_result.error
        # The 'set' action still appears in actions_executed
        executed_types = [a.type for a in fraud_result.actions_executed]
        assert "set" in executed_types


# ===========================================================================
# Test class 6 — compound conditions (and/or/not) in full-stack context
# ===========================================================================


class TestCompoundConditionsEndToEnd:
    """Verify and/or/not compound operators work end-to-end through the service."""

    @pytest.mark.asyncio
    async def test_all_of_compound_condition(self) -> None:
        """A rule with all_of fires only when ALL conditions are met."""
        compound_rule = (
            rule("vip")
            .priority(5)
            .when(
                all_of(
                    field("customer.tier").eq("gold"),
                    field("order.total").ge(500),
                )
            )
            .then(set_action("discount", 20))
            .otherwise(set_action("discount", 0))
            .build()
        )
        rs = ruleset("compound-rs").add(compound_rule).build()
        assert validate_ruleset(rs) == []

        repo = InMemoryRuleSetRepository()
        svc = RuleEngineService(repository=repo)
        await svc.save_ruleset(rs)

        # both conditions met
        ctx_match: dict[str, Any] = {"customer": {"tier": "gold"}, "order": {"total": 600}}
        await svc.evaluate_by_name("compound-rs", ctx_match)
        assert ctx_match["discount"] == 20

        # only one condition met
        ctx_no_match: dict[str, Any] = {"customer": {"tier": "gold"}, "order": {"total": 100}}
        await svc.evaluate_by_name("compound-rs", ctx_no_match)
        assert ctx_no_match["discount"] == 0
