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
"""Conditions actuator endpoint — Spring Boot ``/actuator/conditions`` parity.

Reports the auto-configuration condition evaluation: which ``@auto_configuration``
classes matched (positiveMatches) vs were excluded (negativeMatches), by
re-evaluating their ``@conditional_on_*`` decorators against the live context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


def _describe(cond: dict[str, Any]) -> str:
    """Human-readable summary of a single condition dict."""
    ctype = cond.get("type", "?")
    if ctype == "on_property":
        having = cond.get("having_value")
        return f"@conditional_on_property({cond.get('key')}{'=' + having if having else ''})"
    if ctype == "on_class":
        return "@conditional_on_class"
    if ctype == "on_bean":
        return f"@conditional_on_bean({getattr(cond.get('bean_type'), '__name__', cond.get('bean_type'))})"
    if ctype == "on_missing_bean":
        return f"@conditional_on_missing_bean({getattr(cond.get('bean_type'), '__name__', cond.get('bean_type'))})"
    return f"@conditional({ctype})"


class ConditionsEndpoint:
    """Exposes the condition evaluation report at ``/actuator/conditions``."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "conditions"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        positive: dict[str, list[dict[str, str]]] = {}
        negative: dict[str, dict[str, list[dict[str, str]]]] = {}
        unconditional: list[str] = []

        evaluator = self._make_evaluator()
        for cls in self._auto_configuration_classes():
            conditions: list[dict[str, Any]] = getattr(cls, "__pyfly_conditions__", [])
            single = getattr(cls, "__pyfly_condition__", None)
            if not conditions and single is None:
                unconditional.append(cls.__name__)
                continue

            matched, not_matched = self._evaluate(cls, conditions, single, evaluator)
            if not_matched:
                negative[cls.__name__] = {
                    "notMatched": [{"condition": c, "message": "did not match"} for c in not_matched],
                    "matched": [{"condition": c, "message": "matched"} for c in matched],
                }
            else:
                positive[cls.__name__] = [{"condition": c, "message": "matched"} for c in matched]

        return {
            "contexts": {
                "application": {
                    "positiveMatches": positive,
                    "negativeMatches": negative,
                    "unconditionalClasses": unconditional,
                }
            }
        }

    def _evaluate(
        self, cls: type, conditions: list[dict[str, Any]], single: Any, evaluator: Any
    ) -> tuple[list[str], list[str]]:
        matched: list[str] = []
        not_matched: list[str] = []

        if single is not None:
            try:
                (matched if single() else not_matched).append("@conditional (stereotype)")
            except Exception:  # noqa: BLE001
                not_matched.append("@conditional (stereotype)")

        for cond in conditions:
            desc = _describe(cond)
            try:
                ok = evaluator._evaluate(cond, declaring_cls=cls) if evaluator else True
            except Exception:  # noqa: BLE001
                ok = False
            (matched if ok else not_matched).append(desc)

        return matched, not_matched

    def _make_evaluator(self) -> Any | None:
        try:
            from pyfly.context.condition_evaluator import ConditionEvaluator

            return ConditionEvaluator(self._context.config, self._context.container)
        except Exception:  # noqa: BLE001
            return None

    def _auto_configuration_classes(self) -> list[type]:
        try:
            from pyfly.config.auto import discover_auto_configurations

            return sorted(discover_auto_configurations(), key=lambda c: c.__name__)
        except Exception:  # noqa: BLE001
            return []
