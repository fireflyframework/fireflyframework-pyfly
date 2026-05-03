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

import os
from typing import Any

from pyfly.core.config import Config


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

        Supports:
        - Simple profiles: "dev" matches if "dev" is active
        - Negation: "!production" matches if "production" is NOT active
        - Comma-separated: "dev,test" matches if "dev" OR "test" is active
        """
        return any(self._matches_profile_expression(expr) for expr in profiles)

    def _matches_profile_expression(self, expr: str) -> bool:
        """Evaluate a single profile expression."""
        if "," in expr:
            sub_profiles = [p.strip() for p in expr.split(",") if p.strip()]
            return any(self._matches_single(p) for p in sub_profiles)
        return self._matches_single(expr)

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
