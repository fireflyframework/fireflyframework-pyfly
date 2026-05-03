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
"""Saga engine configuration properties.

Plain dataclasses holding saga and backpressure settings.
Auto-configuration will bind these to YAML config later.

YAML structure::

    pyfly:
      transactional:
        saga:
          enabled: true
          compensation_policy: STRICT_SEQUENTIAL
          default_timeout_ms: 300000
          max_concurrent_sagas: 100
          layer_concurrency: 0
          persistence_enabled: true
          metrics_enabled: true
          recovery_enabled: true
          recovery_interval_seconds: 60
          stale_threshold_seconds: 600
          cleanup_older_than_hours: 24
        backpressure:
          strategy: adaptive
          concurrency: 10
          batch_size: 5
          failure_threshold: 50
          success_threshold: 2
          wait_duration_ms: 60000
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SagaEngineProperties:
    """Configuration for the saga engine."""

    enabled: bool = True
    compensation_policy: str = "STRICT_SEQUENTIAL"
    default_timeout_ms: int = 300_000
    max_concurrent_sagas: int = 100
    layer_concurrency: int = 0  # 0 = unlimited
    persistence_enabled: bool = True
    metrics_enabled: bool = True
    recovery_enabled: bool = True
    recovery_interval_seconds: int = 60
    stale_threshold_seconds: int = 600
    cleanup_older_than_hours: int = 24


@dataclass
class BackpressureProperties:
    """Configuration for backpressure strategies."""

    strategy: str = "adaptive"
    concurrency: int = 10
    batch_size: int = 5
    failure_threshold: int = 50
    success_threshold: int = 2
    wait_duration_ms: int = 60_000
