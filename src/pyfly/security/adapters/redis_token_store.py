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
"""Redis-backed OAuth2 token store (``pyfly.security.oauth2.token-store.provider=redis``).

Cross-instance storage of refresh tokens, authorization codes, rotation families and pushed authorization
requests, with fast distributed revocation. It is an
:class:`~pyfly.security.oauth2.authorization_server.AtomicTokenStore`: every grant operation is one Lua
script, which Redis runs atomically, so a code or refresh token is consumed once however many requests or
instances present it, a rotation mints its token only while the family is active, and a revocation (which
deletes the family's tokens) is never undone by a rotation in flight.

Layout, under *key_prefix*: a record is a hash at ``<prefix><kind>:<id>`` (``client_id``, ``expires_at``,
``used``, ``family_id``, ``data``); a family is a hash at ``<prefix>family:<id>`` (``client_id``,
``active``, ``expires_at``) with the set of its refresh tokens at ``<prefix>family:<id>:tokens``. Every key
expires at its record's expiry plus *purge_grace*, so nothing is kept forever. A revocation and a replay
reach keys the presented token does not name, so on Redis Cluster the store needs all of its keys on one
shard (a single-shard deployment).

Hexagonal: the async Redis client is injected by the composition root; this module never imports
``redis``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pyfly.security.oauth2.authorization_server import AUTHORIZATION_CODE, GrantOutcome, TokenRecord

_REVOKE = """
local function revoke(prefix, family_id)
  local family = prefix .. 'family:' .. family_id
  if redis.call('EXISTS', family) == 1 then
    redis.call('HSET', family, 'active', '0')
  end
  local members = family .. ':tokens'
  for _, token_id in ipairs(redis.call('SMEMBERS', members)) do
    redis.call('DEL', prefix .. 'refresh_token:' .. token_id)
  end
  redis.call('DEL', members)
end
"""

_STORE_TOKEN = """
local function store_token(key, members, family, token_id, client_id, expires_at, family_id, data, expire_at)
  redis.call('HSET', key, 'client_id', client_id, 'expires_at', expires_at, 'used', '0',
    'family_id', family_id, 'data', data)
  redis.call('EXPIREAT', key, expire_at)
  redis.call('SADD', members, token_id)
  local current = tonumber(redis.call('HGET', family, 'expires_at') or '0')
  if tonumber(expires_at) > current then
    redis.call('HSET', family, 'expires_at', expires_at)
    redis.call('EXPIREAT', family, expire_at)
    redis.call('EXPIREAT', members, expire_at)
  end
end
"""

# KEYS: record. ARGV: client_id, expires_at, used, family_id, data, expire_at.
_SAVE = """
redis.call('HSET', KEYS[1], 'client_id', ARGV[1], 'expires_at', ARGV[2], 'used', ARGV[3],
  'family_id', ARGV[4], 'data', ARGV[5])
redis.call('EXPIREAT', KEYS[1], ARGV[6])
return 1
"""

# KEYS: record. ARGV: prefix. Returns {fields, family_active} or nil.
_LOAD = """
local fields = redis.call('HGETALL', KEYS[1])
if #fields == 0 then
  return nil
end
local family_id = redis.call('HGET', KEYS[1], 'family_id')
local active = '1'
if family_id and family_id ~= '' then
  active = redis.call('HGET', ARGV[1] .. 'family:' .. family_id, 'active') or '0'
end
return {fields, active}
"""

# KEYS: token, family, members. ARGV: token_id, client_id, expires_at, family_id, data, expire_at.
_ISSUE = (
    _STORE_TOKEN
    + """
redis.call('HSET', KEYS[2], 'client_id', ARGV[2], 'active', '1', 'expires_at', '0')
store_token(KEYS[1], KEYS[3], KEYS[2], ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6])
return 1
"""
)

# KEYS: code, token, family, members.
# ARGV: prefix, now, token_id, client_id, expires_at, family_id, data, expire_at.
_REDEEM = (
    _REVOKE
    + _STORE_TOKEN
    + """
local code = KEYS[1]
if redis.call('EXISTS', code) == 0 then
  return 'unknown'
end
if redis.call('HGET', code, 'used') == '1' then
  local issued = redis.call('HGET', code, 'family_id')
  if issued and issued ~= '' then
    revoke(ARGV[1], issued)
  end
  return 'replayed'
end
if tonumber(redis.call('HGET', code, 'expires_at')) < tonumber(ARGV[2]) then
  return 'expired'
end
redis.call('HSET', code, 'used', '1', 'family_id', ARGV[6])
redis.call('HSET', KEYS[3], 'client_id', ARGV[4], 'active', '1', 'expires_at', '0')
store_token(KEYS[2], KEYS[4], KEYS[3], ARGV[3], ARGV[4], ARGV[5], ARGV[6], ARGV[7], ARGV[8])
return 'granted'
"""
)

# KEYS: presented token, family, members, new token.
# ARGV: prefix, now, token_id, client_id, expires_at, family_id, data, expire_at.
_ROTATE = (
    _REVOKE
    + _STORE_TOKEN
    + """
local presented = KEYS[1]
if redis.call('EXISTS', presented) == 0 or redis.call('HGET', presented, 'family_id') ~= ARGV[6] then
  return 'unknown'
end
if redis.call('HGET', KEYS[2], 'active') ~= '1' then
  return 'revoked'
end
if redis.call('HGET', presented, 'used') == '1' then
  revoke(ARGV[1], ARGV[6])
  return 'replayed'
end
if tonumber(redis.call('HGET', presented, 'expires_at')) < tonumber(ARGV[2]) then
  return 'expired'
end
redis.call('HSET', presented, 'used', '1')
store_token(KEYS[4], KEYS[3], KEYS[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6], ARGV[7], ARGV[8])
return 'granted'
"""
)

# KEYS: record. ARGV: client_id, now. Returns the record's fields, or nil.
_TAKE = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('HGET', KEYS[1], 'client_id') ~= ARGV[1] then
  return nil
end
if tonumber(redis.call('HGET', KEYS[1], 'expires_at')) < tonumber(ARGV[2]) then
  return nil
end
local fields = redis.call('HGETALL', KEYS[1])
redis.call('DEL', KEYS[1])
return fields
"""

# ARGV: prefix, family_id.
_REVOKE_FAMILY = (
    _REVOKE
    + """
revoke(ARGV[1], ARGV[2])
return 1
"""
)

_SCRIPTS = {
    "save": _SAVE,
    "load": _LOAD,
    "issue": _ISSUE,
    "redeem": _REDEEM,
    "rotate": _ROTATE,
    "take": _TAKE,
    "revoke_family": _REVOKE_FAMILY,
}


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class RedisTokenStore:
    """OAuth2 token store over an injected async Redis client (see the module documentation).

    Args:
        client: A ``redis.asyncio`` client.
        ttl: Kept for compatibility and unused: each record expires at its own expiry plus *purge_grace*.
        key_prefix: The prefix of every key the store writes.
        purge_grace: How long a record is kept after it expired (a used token stays long enough for a late
            replay to still revoke its family).
    """

    def __init__(
        self,
        client: Any,
        *,
        ttl: int | None = None,
        key_prefix: str = "pyfly:oauth2:token:",
        purge_grace: timedelta = timedelta(hours=1),
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._prefix = key_prefix
        self.purge_grace = purge_grace
        self._scripts: dict[str, Any] = {}

    @property
    def key_prefix(self) -> str:
        """The prefix of every key the store writes."""
        return self._prefix

    def _key(self, kind: str, token_id: str) -> str:
        return f"{self._prefix}{kind}:{token_id}"

    def _family_keys(self, family_id: str) -> tuple[str, str]:
        family = f"{self._prefix}family:{family_id}"
        return family, f"{family}:tokens"

    def _expire_at(self, expires_at: int) -> int:
        return expires_at + int(self.purge_grace.total_seconds())

    async def _run(self, name: str, keys: list[str], args: list[Any]) -> Any:
        script = self._scripts.get(name)
        if script is None:
            script = self._scripts[name] = self._client.register_script(_SCRIPTS[name])
        return await script(keys=keys, args=args)

    def _token_args(self, token: TokenRecord) -> list[Any]:
        return [
            token.token_id,
            token.client_id,
            token.expires_at,
            token.family_id or "",
            json.dumps(token.data),
            self._expire_at(token.expires_at),
        ]

    @staticmethod
    def _record(kind: str, token_id: str, fields: list[Any], *, family_active: bool = True) -> TokenRecord:
        values = {_text(fields[index]): _text(fields[index + 1]) for index in range(0, len(fields), 2)}
        return TokenRecord(
            token_id=token_id,
            kind=kind,
            client_id=values.get("client_id", ""),
            expires_at=int(values.get("expires_at", "0")),
            data=json.loads(values.get("data", "{}")),
            family_id=values.get("family_id") or None,
            used=values.get("used") == "1",
            family_active=family_active,
        )

    # ------------------------------------------------------------------
    # AtomicTokenStore
    # ------------------------------------------------------------------

    async def save(self, record: TokenRecord) -> None:
        await self._run(
            "save",
            [self._key(record.kind, record.token_id)],
            [
                record.client_id,
                record.expires_at,
                "1" if record.used else "0",
                record.family_id or "",
                json.dumps(record.data),
                self._expire_at(record.expires_at),
            ],
        )

    async def load(self, kind: str, token_id: str) -> TokenRecord | None:
        found = await self._run("load", [self._key(kind, token_id)], [self._prefix])
        if not found:
            return None
        fields, active = found
        return self._record(kind, token_id, list(fields), family_active=_text(active) == "1")

    async def issue(self, token: TokenRecord) -> None:
        family, members = self._family_keys(str(token.family_id))
        await self._run("issue", [self._key(token.kind, token.token_id), family, members], self._token_args(token))

    async def redeem(self, code: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        family, members = self._family_keys(str(token.family_id))
        keys = [self._key(AUTHORIZATION_CODE, code), self._key(token.kind, token.token_id), family, members]
        outcome = await self._run("redeem", keys, [self._prefix, now, *self._token_args(token)])
        return GrantOutcome(_text(outcome))

    async def rotate(self, token_id: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        family, members = self._family_keys(str(token.family_id))
        keys = [self._key(token.kind, token_id), family, members, self._key(token.kind, token.token_id)]
        outcome = await self._run("rotate", keys, [self._prefix, now, *self._token_args(token)])
        return GrantOutcome(_text(outcome))

    async def take(self, kind: str, token_id: str, *, client_id: str, now: int) -> TokenRecord | None:
        fields = await self._run("take", [self._key(kind, token_id)], [client_id, now])
        if not fields:
            return None
        return self._record(kind, token_id, list(fields))

    async def revoke_family(self, family_id: str) -> None:
        await self._run("revoke_family", [], [self._prefix, family_id])
