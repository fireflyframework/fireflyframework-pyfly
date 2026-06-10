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
"""Tests for the pluggable action-handler registry (SP-13 Part B Item 1)."""

from __future__ import annotations

from typing import Any

from pyfly.rule_engine.dsl import Action, Rule
from pyfly.rule_engine.evaluator import RuleEvaluator


class TestCustomHandlerInjection:
    def test_custom_call_handler_is_invoked_and_mutates_ctx(self) -> None:
        """A ``call``-type action registered at construction is executed."""

        def _call_handler(action: Action, ctx: dict[str, Any]) -> None:
            # Simulate an RPC result being written back into context
            ctx["call_result"] = f"called:{action.target}"

        evaluator = RuleEvaluator(action_handlers={"call": _call_handler})
        rule = Rule(id="r", then=[Action(type="call", target="my_service")])
        ctx: dict[str, Any] = {}
        result = evaluator.evaluate(rule, ctx)

        assert ctx["call_result"] == "called:my_service"
        assert result.error is None
        assert [a.type for a in result.actions_executed] == ["call"]

    def test_custom_handler_receives_full_action(self) -> None:
        """The handler receives the complete Action including arguments."""
        received: list[Action] = []

        def _capture(action: Action, ctx: dict[str, Any]) -> None:
            received.append(action)

        evaluator = RuleEvaluator(action_handlers={"http": _capture})
        action = Action(
            type="http",
            target="https://example.com",
            value="POST",
            arguments={"body": "hello"},
        )
        rule = Rule(id="r", then=[action])
        evaluator.evaluate(rule, {})

        assert len(received) == 1
        assert received[0].target == "https://example.com"
        assert received[0].arguments == {"body": "hello"}

    def test_builtin_set_still_works_with_custom_handlers(self) -> None:
        """Custom handlers are additive — built-ins remain functional."""
        ran: list[str] = []

        def _noop(action: Action, ctx: dict[str, Any]) -> None:
            ran.append(action.type)

        evaluator = RuleEvaluator(action_handlers={"noop": _noop})
        rule = Rule(
            id="r",
            then=[
                Action(type="set", target="x", value=42),
                Action(type="noop"),
            ],
        )
        ctx: dict[str, Any] = {}
        result = evaluator.evaluate(rule, ctx)

        assert ctx["x"] == 42
        assert "noop" in ran
        assert result.error is None


class TestUnregisteredActionType:
    def test_unregistered_type_raises_not_implemented_and_is_isolated(self) -> None:
        """An unregistered action type raises NotImplementedError (audit #215).

        The error is recorded in the result, and sibling actions still execute
        (audit #216 — isolation preserved).
        """
        rule = Rule(
            id="r",
            then=[
                Action(type="unknown_xyz", target="irrelevant"),
                Action(type="set", target="ok", value=True),
            ],
        )
        ctx: dict[str, Any] = {}
        result = RuleEvaluator().evaluate(rule, ctx)

        assert ctx.get("ok") is True, "sibling 'set' should still execute"
        assert result.error is not None
        assert "unknown_xyz" in result.error
        assert [a.type for a in result.actions_executed] == ["set"]

    def test_custom_evaluator_unregistered_type_still_raises(self) -> None:
        """Even with custom handlers loaded, unknown types still raise."""
        evaluator = RuleEvaluator(action_handlers={"call": lambda a, c: None})
        rule = Rule(id="r", then=[Action(type="calculate", target="x")])
        ctx: dict[str, Any] = {}
        result = evaluator.evaluate(rule, ctx)

        assert result.error is not None
        assert "calculate" in result.error
        assert result.actions_executed == []

    def test_custom_handler_override_replaces_builtin(self) -> None:
        """A custom handler with the same key as a built-in overrides it."""
        shadow_called: list[bool] = []

        def _shadow_set(action: Action, ctx: dict[str, Any]) -> None:
            shadow_called.append(True)
            # intentionally do NOT write to ctx — just prove override worked

        evaluator = RuleEvaluator(action_handlers={"set": _shadow_set})
        rule = Rule(id="r", then=[Action(type="set", target="x", value=99)])
        ctx: dict[str, Any] = {}
        evaluator.evaluate(rule, ctx)

        assert shadow_called, "override should have been called"
        assert "x" not in ctx, "original set logic should NOT have run"
