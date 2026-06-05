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
"""Redis-backed session store."""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
from typing import Any, cast

_logger = logging.getLogger(__name__)

_KEY_PREFIX = "pyfly:session:"
_TYPE_KEY = "__pyfly_type__"

# Tagged dataclass types allowed to be reconstructed from session JSON on read.
# Restricting this prevents an arbitrary-object instantiation gadget if the
# session store is ever attacker-writable. Framework types are pre-registered;
# applications opt their own session-stored dataclasses in via allow_session_type.
_ALLOWED_TYPE_TAGS: set[str] = {"pyfly.security.context:SecurityContext"}


def allow_session_type(cls: type) -> None:
    """Allow *cls* (a dataclass) to be rehydrated from the Redis session store.

    Only allowlisted tagged types are reconstructed on read; any other tagged
    value is returned as a plain dict. Call this once at startup for a custom
    dataclass an application stores in the session.
    """
    _ALLOWED_TYPE_TAGS.add(f"{cls.__module__}:{cls.__qualname__}")


def _json_default(obj: Any) -> Any:
    """Encode dataclass session attributes (e.g. SecurityContext) with a type tag.

    Lets non-primitive attributes survive a JSON round-trip so OAuth2 session
    login can persist a SecurityContext to Redis (audit #46).
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = dataclasses.asdict(obj)
        data[_TYPE_KEY] = f"{type(obj).__module__}:{type(obj).__qualname__}"
        return data
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _json_object_hook(d: dict[str, Any]) -> Any:
    """Rehydrate an allowlisted tagged dataclass dict into its original type.

    A tag that is not on the allowlist is NOT imported or instantiated — the
    plain dict is returned instead — to avoid an arbitrary-object instantiation
    gadget from session data.
    """
    tag = d.get(_TYPE_KEY)
    if not tag:
        return d
    payload = {k: v for k, v in d.items() if k != _TYPE_KEY}
    if tag not in _ALLOWED_TYPE_TAGS:
        _logger.warning("Refusing to rehydrate non-allowlisted session type %r", tag)
        return payload
    module_name, _, qualname = tag.partition(":")
    try:
        obj: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        return obj(**payload)
    except Exception:  # pragma: no cover - defensive: fall back to the plain dict
        return payload


class RedisSessionStore:
    """Session store backed by ``redis.asyncio``.

    Values are JSON-serialized before storage.
    Keys are prefixed with ``pyfly:session:`` for namespace isolation.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve and deserialize session data."""
        raw = await self._client.get(self._key(session_id))
        if raw is None:
            return None
        try:
            return cast(dict[str, Any], json.loads(raw, object_hook=_json_object_hook))
        except (json.JSONDecodeError, TypeError):
            _logger.warning("Failed to deserialize session '%s'", session_id)
            return None

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        """Serialize and store session data with a TTL in seconds."""
        raw = json.dumps(data, default=_json_default)
        await self._client.set(self._key(session_id), raw.encode(), ex=ttl)

    async def delete(self, session_id: str) -> None:
        """Remove a session."""
        await self._client.delete(self._key(session_id))

    async def exists(self, session_id: str) -> bool:
        """Check whether a session exists."""
        count = await self._client.exists(self._key(session_id))
        return cast(bool, count > 0)
