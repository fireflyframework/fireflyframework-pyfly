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
"""@Value descriptor for injecting configuration values into bean fields.

Usage::

    @service
    class MyService:
        app_name: str = Value("${pyfly.app.name}")
        timeout: int = Value("${pyfly.timeout:30}")
"""

from __future__ import annotations

import re
from typing import Any

from pyfly.core.config import Config

_PLACEHOLDER_RE = re.compile(r"^\$\{([^}]+)\}$")


class Value:
    """Descriptor that resolves a configuration expression at bean creation time.

    Expressions:
        ``${key}`` — resolve from Config, raise KeyError if missing.
        ``${key:default}`` — resolve from Config, use default if missing.
        ``#{ ... }`` — evaluate a SpEL-lite expression (arithmetic/boolean/ternary,
            with ``${key}`` substitution and an ``env`` mapping).
        ``literal`` — return the string as-is (no ``${}``/``#{}`` wrapper).
    """

    def __init__(self, expression: str) -> None:
        self._expression = expression

    @property
    def expression(self) -> str:
        return self._expression

    def resolve(self, config: Config) -> Any:
        """Resolve the expression against the given Config."""
        from pyfly.core.expression import evaluate, is_expression

        if is_expression(self._expression):
            return evaluate(self._expression, config)

        match = _PLACEHOLDER_RE.match(self._expression)
        if not match:
            return self._expression

        inner = match.group(1)

        # Check for default: ${key:default}
        if ":" in inner:
            key, default = inner.split(":", 1)
            result = config.get(key)
            return default if result is None else result

        # No default — raise if missing
        result = config.get(inner)
        if result is None:
            raise KeyError(
                f"Configuration key '{inner}' not found and no default provided "
                f"in @Value expression '{self._expression}'"
            )
        return result
