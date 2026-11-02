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
"""Validation shared by atomic session claim adapters."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any


class SessionClaimUnavailable(RuntimeError):
    """The configured backend cannot safely commit a session claim."""


def validate_claim(old_id: str, new_id: str, expected: Mapping[str, str], names: tuple[str, ...], ttl: int) -> None:
    if (
        not isinstance(old_id, str)
        or not old_id
        or not isinstance(new_id, str)
        or not new_id
        or old_id == new_id
        or not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or ttl <= 0
        or not isinstance(names, tuple)
        or not names
        or any(not isinstance(name, str) or not name or name.startswith("_") for name in names)
        or len(set(names)) != len(names)
        or not isinstance(expected, Mapping)
        or not expected
        or any(name not in names or not isinstance(value, str) or not value for name, value in expected.items())
    ):
        raise ValueError("Invalid session claim parameters")


def matches(data: Any, expected: Mapping[str, str]) -> bool:
    return isinstance(data, dict) and all(
        isinstance(data.get(name), str) and hmac.compare_digest(data[name].encode(), value.encode())
        for name, value in expected.items()
    )
