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
"""Programmatic TCC builder — alternative to ``@tcc`` / ``@tcc_participant``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pyfly.transactional.tcc.registry.participant_definition import (
    ParticipantDefinition,
)
from pyfly.transactional.tcc.registry.tcc_definition import TccDefinition


class TccBuilder:
    """Construct a :class:`TccDefinition` programmatically."""

    def __init__(self, name: str, *, timeout_ms: int = 30_000) -> None:
        self._definition = TccDefinition(name=name, bean=None, timeout_ms=timeout_ms)

    def participant(
        self,
        participant_id: str,
        *,
        try_method: Callable[..., Awaitable[Any] | Any],
        confirm_method: Callable[..., Awaitable[Any] | Any],
        cancel_method: Callable[..., Awaitable[Any] | Any],
        order: int = 0,
        timeout_ms: int = 0,
        optional: bool = False,
    ) -> TccBuilder:
        participant = ParticipantDefinition(
            id=participant_id,
            order=order,
            timeout_ms=timeout_ms,
            optional=optional,
            try_method=try_method,
            confirm_method=confirm_method,
            cancel_method=cancel_method,
        )
        self._definition.participants[participant_id] = participant
        return self

    def build(self) -> TccDefinition:
        return self._definition
