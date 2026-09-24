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
"""Typed record identities and opaque edit tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from pydantic import TypeAdapter

from pyfly.kernel.exceptions import ConflictException, ValidationException


def json_value(value: Any) -> Any:
    if type(value) is int and abs(value) > 9007199254740991:
        return str(value)
    return TypeAdapter(Any).dump_python(value, mode="json")


def encode_id(values: list[Any]) -> str:
    if len(values) == 1:
        return str(values[0])
    raw = json.dumps([json_value(v) for v in values], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_id(value: str, types: list[Any]) -> list[Any]:
    try:
        if len(value) > 2048:
            raise ValueError("Oversized identifier")
        raw = [value] if len(types) == 1 else json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(raw, list) or len(raw) != len(types):
            raise ValueError("Invalid key")
        return [TypeAdapter(t).validate_python(v) for t, v in zip(types, raw, strict=True)]
    except (ValueError, TypeError) as exc:
        raise ValidationException("Invalid record identifier") from exc


class EditTokens:
    def __init__(self, key: str) -> None:
        self._key = key.encode()

    def issue(self, resource: str, snapshot: Any) -> str:
        payload = json.dumps([resource, snapshot], sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def check(self, resource: str, snapshot: Any, token: str) -> None:
        if len(self._key) < 32:
            raise ValueError("Writes require an edit-token-key of at least 32 characters")
        if (
            not isinstance(token, str)
            or not token.isascii()
            or not hmac.compare_digest(self.issue(resource, snapshot), token)
        ):
            raise ConflictException("This record has changed; reload before saving")
