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
"""TccPhase — enumeration of the three TCC lifecycle phases."""

from __future__ import annotations

from enum import StrEnum


class TccPhase(StrEnum):
    """Phase of a TCC execution.

    A TCC transaction progresses through three phases:

    * **TRY** — tentatively reserve resources.
    * **CONFIRM** — commit all reserved resources.
    * **CANCEL** — release / roll back all reserved resources.
    """

    TRY = "TRY"
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"
