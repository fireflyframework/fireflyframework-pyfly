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
"""TCC engine configuration properties.

Plain dataclass holding TCC settings.
Auto-configuration will bind these to YAML config later.

YAML structure::

    pyfly:
      transactional:
        tcc:
          enabled: true
          default_timeout_ms: 30000
          retry_enabled: true
          max_retries: 3
          backoff_ms: 1000
          persistence_enabled: true
          metrics_enabled: true
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TccEngineProperties:
    """Configuration for the TCC engine."""

    enabled: bool = True
    default_timeout_ms: int = 30_000
    retry_enabled: bool = True
    max_retries: int = 3
    backoff_ms: int = 1_000
    persistence_enabled: bool = True
    metrics_enabled: bool = True
