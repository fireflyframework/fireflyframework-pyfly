# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Pure, order-independent composition of native policy constraints."""

from __future__ import annotations

from collections.abc import Sequence

from pyfly.security.policy import PolicyContext, PolicyDecision, PolicyLimits, PolicyOutcome, PolicySpec


class PolicyEvaluator:
    """Intersect every required policy layer without consulting storage or executing code.

    Hosts own authentication, policy revision selection, durable approvals, and enforcement
    of the effective limits before effects. Invalid values raise validation errors; a missing
    required policy set returns a deny decision.
    """

    def evaluate(self, policies: Sequence[PolicySpec], context: PolicyContext) -> PolicyDecision:
        context = PolicyContext.model_validate(context)
        layers = tuple(PolicySpec.model_validate(policy) for policy in policies)
        limits = context.limits.model_dump()
        denied: set[str] = set()
        approvals = 0
        ttl: int | None = None

        for policy in layers:
            if policy.allowed_principals is not None and context.principal_id not in policy.allowed_principals:
                denied.add("principal_not_allowed")
            if policy.allowed_actions is not None and context.action not in policy.allowed_actions:
                denied.add("action_not_allowed")
            if policy.allowed_target_kinds is not None and context.target_kind not in policy.allowed_target_kinds:
                denied.add("target_kind_not_allowed")
            for name, value in policy.limits.model_dump().items():
                if value is not None:
                    current = limits[name]
                    limits[name] = value if current is None else min(current, value)
            if context.action in policy.approval_required_actions:
                approvals = max(approvals, policy.min_approvals)
                ttl = policy.approval_ttl_seconds if ttl is None else min(ttl, policy.approval_ttl_seconds)

        effective = PolicyLimits.model_validate(limits)
        if not layers:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY, reason_codes=("policy_missing",), effective_limits=effective
            )
        reasons = tuple(
            reason
            for reason in ("principal_not_allowed", "action_not_allowed", "target_kind_not_allowed")
            if reason in denied
        )
        if reasons:
            return PolicyDecision(outcome=PolicyOutcome.DENY, reason_codes=reasons, effective_limits=effective)
        if approvals:
            return PolicyDecision(
                outcome=PolicyOutcome.APPROVAL_REQUIRED,
                reason_codes=("approval_required",),
                effective_limits=effective,
                min_approvals=approvals,
                approval_ttl_seconds=ttl,
            )
        return PolicyDecision(outcome=PolicyOutcome.ALLOW, effective_limits=effective)
