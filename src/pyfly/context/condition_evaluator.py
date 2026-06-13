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
        """Check if any registered bean is the given type, or a subclass of it.

        ``exclude`` (the declaring class) is skipped so a configuration's own bean
        does not satisfy its own ``@conditional_on_(missing_)bean`` during evaluation.
        An exact-type registration (``cls is bean_type``) DOES count — registering a
        bean of exactly ``T`` must satisfy ``@conditional_on_bean(T)`` and must make
        ``@conditional_on_missing_bean(T)`` back off (matching Spring semantics and
        ``_candidate_bean_groups`` below). Previously exact matches were skipped, which
        broke ``@conditional_on_bean(T)`` for any bean registered exactly as ``T`` and
        forced callers to subclass ``T`` purely to be detected.
        """
        for cls in self._container._registrations:
            if cls is exclude:
                continue
            if cls is bean_type:
                return True
            try:
                if issubclass(cls, bean_type):
                    return True
            except TypeError:
                # Protocols with non-method members (e.g. properties) do not
                # support issubclass(); the identity check above already covers
                # an exact-type registration.
                pass
        return False

    def _eval_on_single_candidate(self, cond: dict[str, Any], declaring_cls: type | None = None) -> bool:
        """Spring @ConditionalOnSingleCandidate: match on exactly one candidate, or on a
        unique @primary among several."""
        groups = self._candidate_bean_groups(cond["bean_type"], exclude=declaring_cls)
        if len(groups) == 1:
            return True
        if len(groups) > 1:
            primary_groups = sum(1 for regs in groups.values() if any(self._is_primary(cls, reg) for cls, reg in regs))
            return primary_groups == 1
        return False

    def _candidate_bean_groups(self, bean_type: type, *, exclude: type | None = None) -> dict[Any, list[Any]]:
        """Group registrations assignable to *bean_type* by distinct bean identity.

        Type-only (never resolves instances). Registrations are keyed by their shared
        factory/instance, so an ``@bean``'s interface-alias registration collapses onto its
        concrete one (one impl of an interface counts ONCE). Genuinely distinct beans — e.g.
        a concrete base class AND a registered subclass, where the base is also a ``_bindings``
        key — each retain their own identity and count separately.
        """
        container = self._container
        groups: dict[Any, list[Any]] = {}
        seen: set[type] = set()

        def _consider(cls: type) -> None:
            if cls is exclude or cls in seen:
                return
            reg = container._registrations.get(cls)
            if reg is None:
                return
            seen.add(cls)
            if reg.factory is not None:
                key: Any = ("factory", id(reg.factory))
            elif reg.instance is not None:
                key = ("instance", id(reg.instance))
            else:
                key = ("type", cls)
            groups.setdefault(key, []).append((cls, reg))

        for impl in container._bindings.get(bean_type, []):
            _consider(impl)
        for cls in list(container._registrations):
            if cls is exclude:
                continue
            try:
                if cls is bean_type or issubclass(cls, bean_type):
                    _consider(cls)
            except TypeError:
                # Protocol with non-method members: rely on explicit bindings only.
                pass
        return groups

    def _is_primary(self, cls: type, reg: Any) -> bool:
        """Whether this candidate is primary (class @primary or @bean(primary=True))."""
        return bool(getattr(cls, "__pyfly_primary__", False) or reg.primary)
