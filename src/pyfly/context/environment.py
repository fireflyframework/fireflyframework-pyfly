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
"""Environment — unified property access with profile support."""

from __future__ import annotations

import ast
import os
import re
from typing import Any

from pyfly.core.config import Config

_PROFILE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+|[&|!()]")


def _eval_bool_ast(node: ast.AST) -> bool:
    """Safely evaluate a boolean AST of Constant/BoolOp/UnaryOp(Not) nodes (no ``eval``)."""
    if isinstance(node, ast.Expression):
        return _eval_bool_ast(node.body)
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_bool_ast(v) for v in node.values)
        return any(_eval_bool_ast(v) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_bool_ast(node.operand)
    raise ValueError("Unsupported profile expression")


class Environment:
    """Provides access to configuration properties and active profiles.

    Profiles are loaded from (in priority order):
    1. ``PYFLY_PROFILES_ACTIVE`` environment variable
    2. ``pyfly.profiles.active`` config property
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._active_profiles = self._load_profiles()

    @property
    def active_profiles(self) -> list[str]:
        """Currently active profiles."""
        return list(self._active_profiles)

    def accepts_profiles(self, *profiles: str) -> bool:
        """Return True if any of the given profile expressions match.

        Supports the Spring Boot 2.4+ profile expression grammar:
        - Simple profiles: ``"dev"`` matches if ``"dev"`` is active
        - Negation: ``"!production"`` / ``"!(prod)"``
        - Boolean operators with grouping: ``"prod & cloud"``, ``"prod | qa"``,
          ``"(prod & cloud) | qa"``
        - Comma-separated (legacy pyfly OR): ``"dev,test"``
        """
        return any(self._matches_profile_expression(expr) for expr in profiles)

    def _matches_profile_expression(self, expr: str) -> bool:
        """Evaluate a single profile expression."""
        expr = expr.strip()
        if any(op in expr for op in ("&", "|", "(")):
            return self._eval_boolean_profile(expr)
        if "," in expr:
            sub_profiles = [p.strip() for p in expr.split(",") if p.strip()]
            return any(self._matches_single(p) for p in sub_profiles)
        return self._matches_single(expr)

    def _eval_boolean_profile(self, expr: str) -> bool:
        """Evaluate a boolean profile expression (``&``/``|``/``!``/``()``)."""
        translated: list[str] = []
        for token in _PROFILE_TOKEN_RE.findall(expr):
            if token == "&":
                translated.append(" and ")
            elif token == "|":
                translated.append(" or ")
            elif token == "!":
                translated.append(" not ")
            elif token in ("(", ")"):
                translated.append(token)
            else:
                translated.append("True" if token in self._active_profiles else "False")
        try:
            tree = ast.parse("".join(translated).strip(), mode="eval")
            return _eval_bool_ast(tree)
        except (SyntaxError, ValueError):
            return False

    def _matches_single(self, profile: str) -> bool:
        """Evaluate a single profile token (with optional ! negation)."""
        if profile.startswith("!"):
            return profile[1:] not in self._active_profiles
        return profile in self._active_profiles

    def get_property(self, key: str, default: Any = None) -> Any:
        """Get a configuration property by dotted key."""
        return self._config.get(key, default)

    def _load_profiles(self) -> list[str]:
        """Load active profiles from env var or config."""
        env_profiles = os.environ.get("PYFLY_PROFILES_ACTIVE", "")
        if env_profiles:
            return [p.strip() for p in env_profiles.split(",") if p.strip()]

        config_profiles = self._config.get("pyfly.profiles.active", "")
        if config_profiles:
            return [p.strip() for p in str(config_profiles).split(",") if p.strip()]

        return []
