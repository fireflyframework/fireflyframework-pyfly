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
"""Condition evaluator — evaluates @conditional_on_* decorators during startup."""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.core.config import Config

logger = logging.getLogger(__name__)

# Condition types that depend on the bean registry (must be evaluated in pass 2).
_BEAN_DEPENDENT_TYPES = frozenset({"on_bean", "on_missing_bean", "on_single_candidate"})


class ConditionEvaluator:
    """Evaluates @conditional_on_* decorators during ApplicationContext startup.

    Uses a two-pass strategy:
    - **Pass 1:** Evaluate conditions independent of the bean registry
      (``on_property``, ``on_class``).
    - **Pass 2:** Evaluate bean-dependent conditions (``on_bean``,
      ``on_missing_bean``) against the surviving set from pass 1.
    """

    def __init__(self, config: Config, container: Container) -> None:
        self._config = config
        self._container = container

    def should_include(self, cls: type, *, bean_pass: bool = False) -> bool:
        """Return True if all conditions on *cls* pass.

        Args:
            cls: The class to evaluate.
            bean_pass: When False (pass 1), only non-bean-dependent conditions
                are checked. When True (pass 2), only bean-dependent conditions
                are checked.
        """
        # Check __pyfly_condition__ (singular callable from stereotype decorators)
        if not bean_pass:
            single = getattr(cls, "__pyfly_condition__", None)
            if single is not None and not single():
                return False

        # Check __pyfly_conditions__ (list of dicts from conditional decorators)
        conditions: list[dict[str, Any]] = getattr(cls, "__pyfly_conditions__", [])
        for cond in conditions:
            is_bean_dep = cond["type"] in _BEAN_DEPENDENT_TYPES
            if is_bean_dep != bean_pass:
                continue  # Skip — belongs to the other pass
            if not self._evaluate(cond, declaring_cls=cls):
                return False

        return True

    def should_include_method(self, method: Any) -> bool:
        """Return True if all conditions on a ``@bean`` method pass.

        Evaluates ``@conditional_on_property`` and ``@conditional_on_class``
        decorators applied directly to ``@bean`` methods within
        ``@configuration`` classes.
        """
        conditions: list[dict[str, Any]] = getattr(method, "__pyfly_conditions__", [])
        return all(self._evaluate(cond) for cond in conditions)

    # ------------------------------------------------------------------
    # Individual condition evaluators
    # ------------------------------------------------------------------

    def _evaluate(self, cond: dict[str, Any], *, declaring_cls: type | None = None) -> bool:
        cond_type = cond["type"]
        if cond_type == "on_property":
            result = self._eval_on_property(cond)
        elif cond_type == "on_class":
            result = cond["check"]()
        elif cond_type == "on_expression":
            result = self._eval_on_expression(cond)
        elif cond_type == "on_web_application":
            result = self._eval_on_web_application()
        elif cond_type == "on_resource":
            result = self._eval_on_resource(cond)
        elif cond_type == "on_missing_bean":
            result = self._eval_on_missing_bean(cond, declaring_cls)
        elif cond_type == "on_bean":
            result = self._eval_on_bean(cond, declaring_cls)
        elif cond_type == "on_single_candidate":
            result = self._eval_on_single_candidate(cond, declaring_cls)
        else:
            logger.warning(
                "unknown_condition_type",
                extra={"condition_type": cond_type, "bean": getattr(declaring_cls, "__name__", None)},
            )
            return True

        if not result and declaring_cls:
            logger.debug(
                "bean_excluded_by_condition",
                extra={
                    "bean": declaring_cls.__name__,
                    "condition_type": cond_type,
                    "condition": {k: v for k, v in cond.items() if k != "check"},
                },
            )
        return result

    def _eval_on_property(self, cond: dict[str, Any]) -> bool:
        value = self._config.get(cond["key"])
        if value is None:
            # Absent property: match only when matchIfMissing is requested.
            return cast(bool, cond.get("match_if_missing", False))
        if cond["having_value"]:
            return cast(bool, str(value).lower() == cond["having_value"].lower())
        # No specific value required: present and not explicitly "false".
        return str(value).strip().lower() != "false"

    def _eval_on_expression(self, cond: dict[str, Any]) -> bool:
        from pyfly.core.expression import evaluate

        return bool(evaluate(cond["expression"], self._config))

    def _eval_on_web_application(self) -> bool:
        """Match when a web stack (Starlette or FastAPI) is importable."""
        return any(importlib.util.find_spec(name) is not None for name in ("starlette", "fastapi"))

    def _eval_on_resource(self, cond: dict[str, Any]) -> bool:
        """Match when the filesystem resource at the configured path exists."""
        return os.path.exists(cond["path"])

    def _eval_on_missing_bean(self, cond: dict[str, Any], declaring_cls: type | None = None) -> bool:
        return not self._has_bean_of_type(cond["bean_type"], exclude=declaring_cls)

    def _eval_on_bean(self, cond: dict[str, Any], declaring_cls: type | None = None) -> bool:
        return self._has_bean_of_type(cond["bean_type"], exclude=declaring_cls)

    def _has_bean_of_type(self, bean_type: type, *, exclude: type | None = None) -> bool:
        """Check if any registered bean is a subclass of the given type."""
        for cls in self._container._registrations:
            if cls is bean_type or cls is exclude:
                continue
            try:
                if issubclass(cls, bean_type):
                    return True
            except TypeError:
                # Protocols with non-method members (e.g. properties) do not
                # support issubclass().  Fall back to identity check only.
                pass
        return False

    def _eval_on_single_candidate(self, cond: dict[str, Any], declaring_cls: type | None = None) -> bool:
        """Spring @ConditionalOnSingleCandidate: match on exactly one candidate, or on a
        unique @primary among several."""
        impls = self._candidate_impls(cond["bean_type"], exclude=declaring_cls)
        if len(impls) == 1:
            return True
        if len(impls) > 1:
            return sum(1 for cls in impls if self._is_primary(cls)) == 1
        return False

    def _candidate_impls(self, bean_type: type, *, exclude: type | None = None) -> set[type]:
        """Distinct concrete impl types assignable to *bean_type*, deduped by impl identity.

        Type-only (never resolves instances). Interface-alias registrations — a reg whose
        key is an interface that has explicit bindings — are skipped; their concrete impls
        are counted via ``_bindings`` so one impl of an interface counts once, not twice.
        """
        container = self._container
        impls: set[type] = set()
        # (1) Concrete impls explicitly bound to the interface/base type.
        for impl in container._bindings.get(bean_type, []):
            if impl is not exclude and impl in container._registrations:
                impls.add(impl)
        # (2) Registrations assignable to bean_type, excluding interface-alias regs.
        for cls in container._registrations:
            if cls is exclude or (container._bindings.get(cls)):
                # `cls` is an interface with bindings -> its reg is an alias (impls
                # already counted in step 1). Skip to avoid double-counting.
                continue
            try:
                if cls is bean_type or issubclass(cls, bean_type):
                    impls.add(cls)
            except TypeError:
                # Protocol with non-method members: rely on explicit bindings only.
                pass
        return impls

    def _is_primary(self, cls: type) -> bool:
        """Whether *cls* is the primary candidate (class @primary or @bean(primary=True))."""
        reg = self._container._registrations.get(cls)
        return bool(getattr(cls, "__pyfly_primary__", False) or (reg is not None and reg.primary))
