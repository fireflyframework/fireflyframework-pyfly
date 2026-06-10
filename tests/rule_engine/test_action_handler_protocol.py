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
"""The ActionHandler SPI is a __call__ protocol: a plain function AND a __call__
object both satisfy it and work as registered handlers (review fix — the protocol
shape now matches the registry's Callable shape)."""

from __future__ import annotations

from typing import Any

from pyfly.rule_engine.dsl import Action, Rule
from pyfly.rule_engine.evaluator import RuleEvaluator
from pyfly.rule_engine.ports.outbound import ActionHandler


def _plain_handler(action: Action, ctx: dict[str, Any]) -> None:
    ctx["fired"] = action.target


class _CallableHandler:
    def __call__(self, action: Action, ctx: dict[str, Any]) -> None:
        ctx["fired"] = action.target


def test_plain_function_satisfies_action_handler_protocol() -> None:
    # runtime_checkable __call__ protocol — any callable qualifies.
    assert isinstance(_plain_handler, ActionHandler)
    assert isinstance(_CallableHandler(), ActionHandler)


def test_plain_function_works_as_registered_handler() -> None:
    evaluator = RuleEvaluator(action_handlers={"call": _plain_handler})
    ctx: dict[str, Any] = {}
    result = evaluator.evaluate(Rule(id="r", then=[Action(type="call", target="audit")]), ctx)
    assert result.error is None
    assert ctx["fired"] == "audit"


def test_callable_object_works_as_registered_handler() -> None:
    evaluator = RuleEvaluator(action_handlers={"call": _CallableHandler()})
    ctx: dict[str, Any] = {}
    result = evaluator.evaluate(Rule(id="r", then=[Action(type="call", target="audit")]), ctx)
    assert result.error is None
    assert ctx["fired"] == "audit"
