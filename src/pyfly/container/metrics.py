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
"""Per-bean runtime metrics collected by the DI container."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BeanMetrics:
    """Metrics collected for a single bean registration."""

    creation_time_ns: int = 0
    resolution_count: int = 0
    created_at: float | None = None
