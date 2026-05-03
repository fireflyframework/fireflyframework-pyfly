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
"""EDA types and data structures."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class EventEnvelope:
    """Envelope wrapping an event with metadata."""

    event_type: str
    payload: dict[str, Any]
    destination: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    headers: dict[str, str] = field(default_factory=dict)


class ErrorStrategy(Enum):
    """Strategy for handling errors during event processing."""

    IGNORE = "IGNORE"
    LOG_AND_CONTINUE = "LOG_AND_CONTINUE"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"
    FAIL_FAST = "FAIL_FAST"
