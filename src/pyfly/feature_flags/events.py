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
"""Application events of the feature-flag subsystem (spec 4.9), published through ``ApplicationEventPublisher``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["FeatureFlagEvaluated", "FeatureFlagUpdated", "FeatureFlagsChanged"]


@dataclass(frozen=True)
class FeatureFlagsChanged:
    """The effective flag set changed: ``changed_keys`` (sorted); ``origin`` is the source that changed it."""

    changed_keys: tuple[str, ...]
    origin: str


@dataclass(frozen=True)
class FeatureFlagUpdated:
    """A store write committed: ``action`` is ``put`` or ``delete``; ``previous``/``current`` are definitions."""

    key: str
    action: str
    actor: str | None
    previous: dict[str, Any] | None
    current: dict[str, Any] | None


@dataclass(frozen=True)
class FeatureFlagEvaluated:
    """One evaluation (exposure record), published only with ``pyfly.feature-flags.events.evaluations=true``."""

    key: str
    value: Any
    variant: str | None
    reason: str
    error_code: str | None
    targeting_key: str | None
