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
"""Flag functions for views rendered through the web layer's template context processors.

They evaluate against the ambient context of the request being rendered. Direct calls to
``TemplateEngine.render`` do not run template context processors.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.feature_flags.client import FeatureFlags

__all__ = ["FeatureFlagsTemplateContext"]


class FeatureFlagsTemplateContext:
    """Contribute ``feature_flag`` and ``feature_variant`` to rendered views."""

    def __init__(self, flags: FeatureFlags) -> None:
        self._flags = flags

    async def get_context(self, request: Any) -> Mapping[str, Any]:
        return {"feature_flag": self.feature_flag, "feature_variant": self.feature_variant}

    def feature_flag(self, key: str, default: bool = False) -> bool:
        return self._flags.is_enabled(key, default=default)

    def feature_variant(self, key: str) -> str | None:
        return self._flags.variant(key)
