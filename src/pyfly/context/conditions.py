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
"""Conditional bean decorators — control when beans are registered."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TypeVar

from pyfly.container.types import Scope

F = TypeVar("F")
T = TypeVar("T", bound=type)


def conditional_on_property(key: str, having_value: str = "", *, match_if_missing: bool = False) -> Callable[[F], F]:
    """Only register this bean if the given config property matches.

    Mirrors Spring Boot's ``@ConditionalOnProperty``:

    - With ``having_value`` set, matches only when the property equals it
      (case-insensitive).
    - Without ``having_value``, matches when the property is present and not
      ``"false"`` (so ``key: false`` becomes an opt-out switch).
    - When the property is absent, matches iff ``match_if_missing`` is True.

    Evaluated at ApplicationContext startup against the active Environment.
    """

    def decorator(cls: F) -> F:
        conditions = list(cls.__dict__.get("__pyfly_conditions__", []))
        conditions.append(
            {
                "type": "on_property",
                "key": key,
                "having_value": having_value,
                "match_if_missing": match_if_missing,
            }
        )
        cls.__pyfly_conditions__ = conditions  # type: ignore[attr-defined]
        return cls

    return decorator


def conditional_on_expression(expression: str) -> Callable[[F], F]:
    """Only register this bean if a SpEL-lite ``#{ ... }`` expression is truthy.

    Mirrors Spring Boot's ``@ConditionalOnExpression``. The expression supports
    arithmetic/comparison/boolean/ternary, ``${key:default}`` config-placeholder
    substitution, and an ``env`` mapping — e.g.
    ``@conditional_on_expression("#{${pyfly.workers:1} > 1}")``. Evaluated against the
    active config at ApplicationContext startup.
    """

    def decorator(cls: F) -> F:
        conditions = list(cls.__dict__.get("__pyfly_conditions__", []))
        conditions.append({"type": "on_expression", "expression": expression})
        cls.__pyfly_conditions__ = conditions  # type: ignore[attr-defined]
        return cls

    return decorator


def conditional_on_class(module_name: str) -> Callable[[F], F]:
    """Only register this bean if the given module is importable.

    Mirrors Spring Boot's @ConditionalOnClass.
    """

    def _check() -> bool:
        try:
            importlib.import_module(module_name)
            return True
        except ImportError:
            return False

    def decorator(cls: F) -> F:
        conditions = list(cls.__dict__.get("__pyfly_conditions__", []))
        conditions.append(
            {
                "type": "on_class",
                "module_name": module_name,
                "check": _check,
            }
        )
        cls.__pyfly_conditions__ = conditions  # type: ignore[attr-defined]
        return cls

    return decorator


def conditional_on_missing_bean(bean_type: type) -> Callable[[F], F]:
    """Only register this bean if no other bean of the given type exists.

    Evaluated at ApplicationContext startup after initial registration.
    """

    def decorator(cls: F) -> F:
        conditions = list(cls.__dict__.get("__pyfly_conditions__", []))
        conditions.append(
            {
                "type": "on_missing_bean",
                "bean_type": bean_type,
            }
        )
        cls.__pyfly_conditions__ = conditions  # type: ignore[attr-defined]
        return cls

    return decorator


def conditional_on_bean(bean_type: type) -> Callable[[F], F]:
    """Only register this bean if another bean of the given type exists."""

    def decorator(cls: F) -> F:
        conditions = list(cls.__dict__.get("__pyfly_conditions__", []))
        conditions.append({"type": "on_bean", "bean_type": bean_type})
        cls.__pyfly_conditions__ = conditions  # type: ignore[attr-defined]
        return cls

    return decorator


def auto_configuration(cls: T) -> T:
    """Mark a @configuration class as auto-configuration.

    Auto-configuration classes:
    - Are processed AFTER user @configuration classes
    - Get implicit @order(1000) (lower priority)
    - Work with @conditional_on_* decorators
    """
    cls.__pyfly_auto_configuration__ = True  # type: ignore[attr-defined]
    cls.__pyfly_injectable__ = True  # type: ignore[attr-defined]
    cls.__pyfly_stereotype__ = "configuration"  # type: ignore[attr-defined]
    if not hasattr(cls, "__pyfly_scope__"):
        cls.__pyfly_scope__ = Scope.SINGLETON  # type: ignore[attr-defined]
    if not hasattr(cls, "__pyfly_order__"):
        cls.__pyfly_order__ = 1000  # type: ignore[attr-defined]
    return cls
