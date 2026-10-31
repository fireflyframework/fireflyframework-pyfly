# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Policy evaluation is application-owned and works without Agentic dependencies."""

from dataclasses import dataclass

from pyfly.container import bean, configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.security import PolicyContext, PolicyDecision, PolicyEvaluator, PolicySpec


@dataclass
class _Admission:
    evaluator: PolicyEvaluator
    policy: PolicySpec

    def check(self, principal: str) -> PolicyDecision:
        return self.evaluator.evaluate(
            [self.policy], PolicyContext(principal_id=principal, action="run", target_kind="architecture")
        )


@configuration
class _PolicyConfiguration:
    @bean
    def evaluator(self) -> PolicyEvaluator:
        return PolicyEvaluator()

    @bean
    def policy(self, config: Config) -> PolicySpec:
        return PolicySpec.model_validate(config.get("policy"))

    @bean
    def admission(self, evaluator: PolicyEvaluator, policy: PolicySpec) -> _Admission:
        return _Admission(evaluator, policy)


async def test_native_di_keeps_policy_admission_isolated_between_applications() -> None:
    alice = ApplicationContext(Config({"policy": {"allowed_principals": ["alice"]}}), auto_configurations=[])
    bob = ApplicationContext(Config({"policy": {"allowed_principals": ["bob"]}}), auto_configurations=[])
    alice.register_bean(_PolicyConfiguration)
    bob.register_bean(_PolicyConfiguration)
    await alice.start()
    try:
        await bob.start()
        try:
            first = alice.get_bean(_Admission)
            second = bob.get_bean(_Admission)
            assert first.check("alice").outcome == "allow"
            assert first.check("bob").outcome == "deny"
            assert second.check("bob").outcome == "allow"
            assert second.check("alice").outcome == "deny"
        finally:
            await bob.stop()
    finally:
        await alice.stop()
