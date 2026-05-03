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
"""PyFly TCC — Try-Confirm/Cancel distributed transactions."""

from __future__ import annotations

from pyfly.transactional.tcc.annotations import (
    FromTry,
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.core.phase import TccPhase
from pyfly.transactional.tcc.core.result import ParticipantResult, TccResult
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine

__all__ = [
    "FromTry",
    "ParticipantResult",
    "TccContext",
    "TccEngine",
    "TccPhase",
    "TccResult",
    "cancel_method",
    "confirm_method",
    "tcc",
    "tcc_participant",
    "try_method",
]
