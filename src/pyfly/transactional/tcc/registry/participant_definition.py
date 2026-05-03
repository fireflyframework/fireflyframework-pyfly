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
"""Participant definition — immutable metadata for a single TCC participant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParticipantDefinition:
    """Immutable descriptor holding all metadata for one TCC participant.

    Populated during TCC registration by inspecting
    ``__pyfly_tcc_participant__`` metadata on nested classes and resolving
    ``@try_method``, ``@confirm_method`` and ``@cancel_method`` on each.

    Attributes:
        id: Unique participant identifier within the TCC transaction.
        order: Execution order (lower values execute first).
        timeout_ms: Participant-level timeout in milliseconds (0 = no timeout).
        optional: Whether the participant is optional.
        participant_class: The nested class decorated with ``@tcc_participant``,
            or ``None`` if not resolved.
        try_method: Resolved reference to the try-phase method, or ``None``.
        confirm_method: Resolved reference to the confirm-phase method, or ``None``.
        cancel_method: Resolved reference to the cancel-phase method, or ``None``.
    """

    id: str
    order: int
    timeout_ms: int = 0
    optional: bool = False
    participant_class: type | None = None
    try_method: Any | None = None
    confirm_method: Any | None = None
    cancel_method: Any | None = None
