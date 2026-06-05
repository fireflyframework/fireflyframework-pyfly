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
"""JSON serialization for cached values that tolerates framework types.

Plain ``json.dumps`` raises ``TypeError`` on datetime/Decimal/UUID/set/Pydantic
values — extremely common in this Pydantic-heavy framework — which previously
crashed the cache write path (audit #72). This encoder converts those to a
JSON-safe form so a cache put never raises.
"""

from __future__ import annotations

import datetime
import decimal
import json
import uuid
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    # Pydantic v2 model → JSON-mode dict.
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"Object of type {type(obj).__name__} is not cache-serializable")


def cache_dumps(value: Any) -> bytes:
    """Serialize *value* to JSON bytes, tolerating framework types."""
    return json.dumps(value, default=_default).encode("utf-8")


def cache_loads(raw: Any) -> Any:
    """Deserialize cached JSON bytes/str back to a Python object."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)
