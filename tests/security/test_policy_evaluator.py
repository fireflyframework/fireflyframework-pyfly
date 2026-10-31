# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Closed native policy constraints and monotonic authorization decisions."""

from itertools import permutations

import pytest
from pydantic import ValidationError

from pyfly.security import PolicyContext, PolicyDecision, PolicyEvaluator, PolicyLimits, PolicySpec


def context(**values):
    return PolicyContext.model_validate(
        {"principal_id": "operator", "action": "run", "target_kind": "architecture", **values}
    )


def test_unrestricted_policy_allows_an_exact_host_context() -> None:
    decision = PolicyEvaluator().evaluate([PolicySpec()], context())
    assert decision.outcome == "allow"
    assert decision.reason_codes == ()
    assert decision.min_approvals == 0
    assert decision.approval_ttl_seconds is None


def test_missing_policy_denies_even_when_context_has_limits() -> None:
    decision = PolicyEvaluator().evaluate([], context(limits={"max_requests": 3}))
    assert decision.outcome == "deny"
    assert decision.reason_codes == ("policy_missing",)
    assert decision.effective_limits.max_requests == 3


@pytest.mark.parametrize(
    ("field", "values", "reason"),
    [
        ("allowed_principals", [], "principal_not_allowed"),
        ("allowed_principals", ["other"], "principal_not_allowed"),
        ("allowed_actions", [], "action_not_allowed"),
        ("allowed_actions", ["deploy"], "action_not_allowed"),
        ("allowed_target_kinds", [], "target_kind_not_allowed"),
        ("allowed_target_kinds", ["worker"], "target_kind_not_allowed"),
    ],
)
def test_empty_and_nonmatching_selectors_deny(field, values, reason) -> None:
    decision = PolicyEvaluator().evaluate([PolicySpec.model_validate({field: values})], context())
    assert decision.outcome == "deny"
    assert decision.reason_codes == (reason,)


def test_exact_selectors_allow_without_expanding_other_policy_layers() -> None:
    policy = PolicySpec(
        allowed_principals=frozenset({"operator"}),
        allowed_actions=frozenset({"run"}),
        allowed_target_kinds=frozenset({"architecture"}),
    )
    assert PolicyEvaluator().evaluate([PolicySpec(), policy], context()).outcome == "allow"
    assert PolicyEvaluator().evaluate([policy], context(principal_id="Operator")).outcome == "deny"


def test_deny_overrides_approval_and_allow_in_every_order() -> None:
    policies = [
        PolicySpec(),
        PolicySpec(approval_required_actions=frozenset({"run"}), min_approvals=3),
        PolicySpec(allowed_principals=frozenset()),
    ]
    for ordered in permutations(policies):
        decision = PolicyEvaluator().evaluate(ordered, context())
        assert decision.outcome == "deny"
        assert decision.reason_codes == ("principal_not_allowed",)
        assert decision.min_approvals == 0
        assert decision.approval_ttl_seconds is None


def test_reasons_are_unique_and_stable_across_policy_order() -> None:
    policies = [PolicySpec(allowed_actions=frozenset()), PolicySpec(allowed_principals=frozenset())]
    for ordered in permutations([*policies, policies[0]]):
        decision = PolicyEvaluator().evaluate(ordered, context())
        assert decision.reason_codes == ("principal_not_allowed", "action_not_allowed")


def test_approval_threshold_and_expiry_only_become_stricter() -> None:
    policies = [
        PolicySpec(approval_required_actions=frozenset({"run"}), min_approvals=2, approval_ttl_seconds=300),
        PolicySpec(approval_required_actions=frozenset({"run"}), min_approvals=4, approval_ttl_seconds=7200),
        PolicySpec(min_approvals=5, approval_ttl_seconds=300),
    ]
    for ordered in permutations(policies):
        decision = PolicyEvaluator().evaluate(ordered, context())
        assert decision.outcome == "approval_required"
        assert decision.reason_codes == ("approval_required",)
        assert decision.min_approvals == 4
        assert decision.approval_ttl_seconds == 300
    assert PolicyEvaluator().evaluate(policies, context(action="package")).outcome == "allow"


def test_ceilings_intersect_with_host_baseline_and_keep_zero() -> None:
    policies = [
        PolicySpec(limits=PolicyLimits(max_run_seconds=90, max_requests=10, max_tokens=2000, budget_microusd=30)),
        PolicySpec(limits=PolicyLimits(max_run_seconds=120, max_requests=0, max_tokens=1000)),
        PolicySpec(),
    ]
    for ordered in permutations(policies):
        decision = PolicyEvaluator().evaluate(ordered, context(limits={"max_run_seconds": 60, "budget_microusd": 20}))
        assert decision.effective_limits.model_dump() == {
            "max_run_seconds": 60,
            "max_requests": 0,
            "max_tokens": 1000,
            "budget_microusd": 20,
        }


@pytest.mark.parametrize("field", ["max_run_seconds", "max_requests", "max_tokens", "budget_microusd"])
def test_each_zero_ceiling_remains_zero_after_composition(field) -> None:
    decision = PolicyEvaluator().evaluate(
        [PolicySpec.model_validate({"limits": {field: 0}}), PolicySpec.model_validate({"limits": {field: 3}})],
        context(),
    )
    assert getattr(decision.effective_limits, field) == 0


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, 2, None])
def test_only_integer_schema_version_one_is_supported(value) -> None:
    with pytest.raises(ValidationError):
        PolicySpec.model_validate({"schema_version": value})


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (PolicySpec, {"unknown": True}),
        (PolicySpec, {"limits": {"unknown": 1}}),
        (PolicyContext, {"principal_id": "operator", "action": "run", "target_kind": "architecture", "approved": True}),
        (PolicyDecision, {"outcome": "allow", "effective_limits": {}, "approved": True}),
    ],
)
def test_unknown_fields_cannot_silently_disable_constraints(model, payload) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("field", ["max_run_seconds", "max_requests", "max_tokens", "budget_microusd"])
@pytest.mark.parametrize("value", [True, False, "1", 1.0, -1, 10**20, float("inf"), float("nan")])
def test_limit_numbers_cannot_be_coerced_or_escape_bounds(field, value) -> None:
    with pytest.raises(ValidationError):
        PolicyLimits.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "maximum"),
    [("max_run_seconds", 86400), ("max_requests", 1000000), ("max_tokens", 1000000000), ("budget_microusd", 10**12)],
)
def test_limit_upper_bound_is_accepted_and_excess_rejected(field, maximum) -> None:
    assert getattr(PolicyLimits.model_validate({field: maximum}), field) == maximum
    with pytest.raises(ValidationError):
        PolicyLimits.model_validate({field: maximum + 1})


@pytest.mark.parametrize("field", ["min_approvals", "approval_ttl_seconds"])
@pytest.mark.parametrize("value", [True, False, "300", 300.0, -1, 0, 100000])
def test_approval_numbers_are_strict_and_bounded(field, value) -> None:
    with pytest.raises(ValidationError):
        PolicySpec.model_validate({field: value})


@pytest.mark.parametrize(
    "field", ["allowed_principals", "allowed_actions", "allowed_target_kinds", "approval_required_actions"]
)
@pytest.mark.parametrize("value", ["run", [True], [1], [b"run"], [""], ["run\n"], ["two words"]])
def test_selectors_are_bounded_exact_strings(field, value) -> None:
    with pytest.raises(ValidationError):
        PolicySpec.model_validate({field: value})


@pytest.mark.parametrize("field", ["principal_id", "action", "target_kind"])
@pytest.mark.parametrize("value", [True, 1, b"run", "", "run\n", "two words"])
def test_missing_or_malformed_identity_dimensions_never_match(field, value) -> None:
    with pytest.raises(ValidationError):
        context(**{field: value})


def test_policy_and_decision_remain_immutable_when_inputs_change() -> None:
    actions = ["run"]
    policy = PolicySpec.model_validate({"allowed_actions": actions, "limits": {"max_requests": 2}})
    actions.append("deploy")
    assert PolicyEvaluator().evaluate([policy], context(action="deploy")).outcome == "deny"
    decision = PolicyEvaluator().evaluate([policy], context())
    with pytest.raises(ValidationError):
        decision.outcome = "allow"
    with pytest.raises(ValidationError):
        decision.effective_limits.max_requests = 100
    with pytest.raises(AttributeError):
        policy.allowed_actions.add("deploy")


def test_native_models_round_trip_json_and_expose_closed_schema() -> None:
    policy = PolicySpec.model_validate({"allowed_actions": ["run"], "limits": {"budget_microusd": 0}})
    restored = PolicySpec.model_validate_json(policy.model_dump_json())
    assert restored == policy
    assert isinstance(restored.allowed_actions, frozenset)
    decision = PolicyEvaluator().evaluate([restored], context())
    assert PolicyDecision.model_validate_json(decision.model_dump_json()) == decision
    assert PolicySpec.model_json_schema()["additionalProperties"] is False


def test_unvalidated_model_copies_cannot_bypass_admission_validation() -> None:
    policy = PolicySpec().model_copy(update={"schema_version": True})
    with pytest.raises(ValidationError):
        PolicyEvaluator().evaluate([policy], context())
    request = context().model_copy(update={"principal_id": ""})
    with pytest.raises(ValidationError):
        PolicyEvaluator().evaluate([PolicySpec()], request)
