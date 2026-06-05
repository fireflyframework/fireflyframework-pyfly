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
"""Tests for the session subsystem, including the v26.06.13 hardening:

- session-fixation: ``HttpSession.rotate_id()`` + ``SessionFilter`` store/cookie migration.
- cookie ``Secure`` auto-set over HTTPS (and via ``X-Forwarded-Proto``).
- Redis store rehydration is restricted to an allowlist (no arbitrary-object gadget).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from pyfly.security.context import SecurityContext
from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.adapters.redis import RedisSessionStore, allow_session_type
from pyfly.session.filter import SessionFilter
from pyfly.session.session import HttpSession


# ---------------------------------------------------------------------------
# HttpSession
# ---------------------------------------------------------------------------
class TestHttpSession:
    def test_attribute_roundtrip(self) -> None:
        s = HttpSession("sid", is_new=True)
        s.set_attribute("user", "ada")
        assert s.get_attribute("user") == "ada"
        assert s.modified is True
        s.remove_attribute("user")
        assert s.get_attribute("user") is None

    def test_invalidate(self) -> None:
        s = HttpSession("sid", {"k": "v"})
        s.invalidate()
        assert s.invalidated is True

    def test_rotate_id_assigns_new_id_and_preserves_data(self) -> None:
        s = HttpSession("old-id", {"k": "v"})
        s.rotate_id()
        assert s.id != "old-id"
        assert s.previous_id == "old-id"
        assert s.get_attribute("k") == "v"
        assert s.modified is True

    def test_rotate_id_is_noop_when_invalidated(self) -> None:
        s = HttpSession("old-id")
        s.invalidate()
        s.rotate_id()
        assert s.id == "old-id"
        assert s.previous_id is None


# ---------------------------------------------------------------------------
# InMemorySessionStore
# ---------------------------------------------------------------------------
class TestInMemorySessionStore:
    @pytest.mark.asyncio
    async def test_save_get_delete_exists(self) -> None:
        store = InMemorySessionStore()
        await store.save("sid", {"a": 1}, ttl=60)
        assert await store.get("sid") == {"a": 1}
        assert await store.exists("sid") is True
        await store.delete("sid")
        assert await store.get("sid") is None
        assert await store.exists("sid") is False

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self) -> None:
        assert await InMemorySessionStore().get("nope") is None

    @pytest.mark.asyncio
    async def test_expired_entry_is_evicted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = InMemorySessionStore()
        await store.save("sid", {"a": 1}, ttl=10)
        # Jump past expiry deterministically.
        monkeypatch.setattr("pyfly.session.adapters.memory.time.monotonic", lambda: 10_000_000.0)
        assert await store.get("sid") is None
        assert await store.exists("sid") is False


# ---------------------------------------------------------------------------
# SessionFilter
# ---------------------------------------------------------------------------
def _request(*, cookies: dict[str, str] | None = None, scheme: str = "http", headers: dict[str, str] | None = None):
    return SimpleNamespace(
        cookies=cookies or {},
        url=SimpleNamespace(scheme=scheme),
        headers=headers or {},
        state=SimpleNamespace(),
    )


class _Response:
    def __init__(self) -> None:
        self.set_cookie_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def set_cookie(self, **kwargs: Any) -> None:
        self.set_cookie_calls.append(kwargs)

    def delete_cookie(self, *, key: str) -> None:
        self.deleted.append(key)


class TestSessionFilter:
    @pytest.mark.asyncio
    async def test_new_session_issues_cookie_insecure_over_http(self) -> None:
        store = InMemorySessionStore()
        f = SessionFilter(store=store)
        request = _request()
        response = _Response()

        async def call_next(req: Any) -> _Response:
            req.state.session.set_attribute("hello", "world")
            return response

        await f.do_filter(request, call_next)
        assert len(response.set_cookie_calls) == 1
        cookie = response.set_cookie_calls[0]
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "lax"
        assert cookie["secure"] is False  # plain HTTP dev
        assert await store.get(cookie["value"]) == request.state.session.get_data()

    @pytest.mark.asyncio
    async def test_cookie_secure_over_https(self) -> None:
        f = SessionFilter(store=InMemorySessionStore())
        request = _request(scheme="https")
        response = _Response()

        async def call_next(req: Any) -> _Response:
            req.state.session.set_attribute("x", "1")
            return response

        await f.do_filter(request, call_next)
        assert response.set_cookie_calls[0]["secure"] is True

    @pytest.mark.asyncio
    async def test_cookie_secure_via_forwarded_proto(self) -> None:
        f = SessionFilter(store=InMemorySessionStore())
        request = _request(headers={"x-forwarded-proto": "https"})
        response = _Response()

        async def call_next(req: Any) -> _Response:
            req.state.session.set_attribute("x", "1")
            return response

        await f.do_filter(request, call_next)
        assert response.set_cookie_calls[0]["secure"] is True

    @pytest.mark.asyncio
    async def test_existing_session_is_loaded(self) -> None:
        store = InMemorySessionStore()
        await store.save("existing", {"user": "ada"}, ttl=60)
        f = SessionFilter(store=store)
        request = _request(cookies={"PYFLY_SESSION": "existing"})

        async def call_next(req: Any) -> _Response:
            assert req.state.session.id == "existing"
            assert req.state.session.get_attribute("user") == "ada"
            return _Response()

        await f.do_filter(request, call_next)

    @pytest.mark.asyncio
    async def test_invalidate_deletes_cookie_and_store_entry(self) -> None:
        store = InMemorySessionStore()
        await store.save("existing", {"user": "ada"}, ttl=60)
        f = SessionFilter(store=store)
        request = _request(cookies={"PYFLY_SESSION": "existing"})
        response = _Response()

        async def call_next(req: Any) -> _Response:
            req.state.session.invalidate()
            return response

        await f.do_filter(request, call_next)
        assert "PYFLY_SESSION" in response.deleted
        assert response.set_cookie_calls == []
        assert await store.get("existing") is None

    @pytest.mark.asyncio
    async def test_rotation_migrates_store_and_cookie(self) -> None:
        store = InMemorySessionStore()
        await store.save("fixed-id", {"user": "ada"}, ttl=60)
        f = SessionFilter(store=store)
        request = _request(cookies={"PYFLY_SESSION": "fixed-id"})
        response = _Response()

        async def call_next(req: Any) -> _Response:
            req.state.session.rotate_id()  # e.g. on login
            return response

        await f.do_filter(request, call_next)
        new_id = response.set_cookie_calls[0]["value"]
        assert new_id != "fixed-id"
        assert await store.get("fixed-id") is None  # old (fixed) id no longer resolves
        assert (await store.get(new_id))["user"] == "ada"  # data carried to the new id


# ---------------------------------------------------------------------------
# RedisSessionStore (fake async client — no redis dependency needed)
# ---------------------------------------------------------------------------
class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.kv.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self.kv[key] = value

    async def delete(self, key: str) -> None:
        self.kv.pop(key, None)

    async def exists(self, key: str) -> int:
        return 1 if key in self.kv else 0


class _Tripwire:
    """If the deserialization gadget were active, instantiating this would raise."""

    def __init__(self, **_kwargs: Any) -> None:
        raise AssertionError("non-allowlisted session type was instantiated!")


@dataclass
class _Prefs:
    """Module-level so its tag (module:_Prefs) resolves via importlib on read."""

    theme: str = "dark"


class TestRedisSessionStore:
    @pytest.mark.asyncio
    async def test_security_context_roundtrip(self) -> None:
        client = _FakeRedis()
        store = RedisSessionStore(client=client)
        ctx = SecurityContext(user_id="u-1", roles=["ADMIN"], permissions=["order:read"])
        await store.save("sid", {"_sc": ctx}, ttl=60)

        loaded = await store.get("sid")
        assert isinstance(loaded["_sc"], SecurityContext)
        assert loaded["_sc"].user_id == "u-1"
        assert loaded["_sc"].has_role("ADMIN")

    @pytest.mark.asyncio
    async def test_non_allowlisted_tag_is_not_instantiated(self, caplog: pytest.LogCaptureFixture) -> None:
        client = _FakeRedis()
        store = RedisSessionStore(client=client)
        tag = f"{_Tripwire.__module__}:{_Tripwire.__qualname__}"
        client.kv["pyfly:session:evil"] = json.dumps({"__pyfly_type__": tag, "a": 1}).encode()

        result = await store.get("evil")
        # Returned as a plain dict — _Tripwire was NOT imported or instantiated.
        assert result == {"a": 1}

    @pytest.mark.asyncio
    async def test_allow_session_type_opts_in_a_custom_type(self) -> None:
        allow_session_type(_Prefs)
        client = _FakeRedis()
        store = RedisSessionStore(client=client)
        await store.save("sid", {"prefs": _Prefs(theme="light")}, ttl=60)

        loaded = await store.get("sid")
        assert isinstance(loaded["prefs"], _Prefs)
        assert loaded["prefs"].theme == "light"
