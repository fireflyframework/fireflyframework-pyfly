# Copyright 2026 Firefly Software Foundation
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

"""Portable Lab inputs and independent snapshots for runner invocations."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Annotated

from pydantic import BeforeValidator, ConfigDict, JsonValue, TypeAdapter

_JSON_INPUT = TypeAdapter(str | dict[str, JsonValue], config=ConfigDict(strict=True, hide_input_in_errors=True))


def _validate_input(value: object) -> str | dict[str, JsonValue]:
    # Recursive JsonValue validation rejects cycles and Python-only values before walking numbers.
    validated = _JSON_INPUT.validate_python(value)

    def finite(item: JsonValue) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Lab input must contain finite JSON numbers")
        if isinstance(item, dict):
            for child in item.values():
                finite(child)
        elif isinstance(item, list):
            for child in item:
                finite(child)

    finite(validated)
    return validated


type LabInput = Annotated[str | dict[str, JsonValue], BeforeValidator(_validate_input)]
"""Text or a finite JSON object accepted by mapping-aware Lab runners."""


def snapshot_input(value: LabInput) -> LabInput:
    """Validate before effects and isolate runner mutations from authored inputs."""
    return deepcopy(_validate_input(value))
