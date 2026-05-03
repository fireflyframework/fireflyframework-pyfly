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
"""TCC definition — aggregate root that groups participant definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)


@dataclass
class TccDefinition:
    """Mutable aggregate holding the complete definition of a registered TCC transaction.

    Built incrementally by :class:`TccRegistry` during bean scanning.

    Attributes:
        name: Unique TCC transaction name (from ``@tcc(name=...)``).
        bean: The TCC bean instance whose nested classes implement participants.
        timeout_ms: Global timeout in milliseconds (0 = no timeout).
        retry_enabled: Whether retries are enabled for the TCC transaction.
        max_retries: Maximum number of retry attempts.
        backoff_ms: Base backoff duration in milliseconds.
        participants: Mapping of participant id to its :class:`ParticipantDefinition`,
            ordered by the participant's ``order`` field.
    """

    name: str
    bean: Any
    timeout_ms: int = 0
    retry_enabled: bool = False
    max_retries: int = 0
    backoff_ms: int = 0
    participants: dict[str, ParticipantDefinition] = field(default_factory=dict)
