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
import time
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

    def test_stored_id_is_the_id_the_store_is_known_to_hold(self) -> None:
        assert HttpSession("fresh", is_new=True).stored_id is None
        loaded = HttpSession("loaded", {"k": "v"})
        assert loaded.stored_id == "loaded"
        loaded.rotate_id()
        assert loaded.stored_id == "loaded"  # the new id is not saved yet
        loaded.mark_persisted()
        assert loaded.stored_id == loaded.id

    def test_mark_persisted_clears_the_pending_change_until_the_next_one(self) -> None:
        s = HttpSession("old-id", {"k": "v"})
        s.rotate_id()
        s.mark_persisted()
        assert s.modified is False
        assert s.previous_id == "old-id"  # still tells the request's other filters that the id rotated
        s.set_attribute("k", "w")
        assert s.modified is True


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
    async def test_replace_changes_only_a_session_the_store_holds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = InMemorySessionStore()
        assert await store.replace("gone", {"a": 1}, ttl=60) is False
        assert await store.get("gone") is None

        await store.save("sid", {"a": 1}, ttl=10)
        assert await store.replace("sid", {"a": 2}, ttl=60) is True
        assert await store.get("sid") == {"a": 2}

        now = time.monotonic()
        monkeypatch.setattr("pyfly.session.adapters.memory.time.monotonic", lambda: now + 30)
        assert await store.exists("sid")  # the replace moved the expiry 60 seconds on
        monkeypatch.setattr("pyfly.session.adapters.memory.time.monotonic", lambda: now + 120)
        assert await store.replace("sid", {"a": 3}, ttl=60) is False  # expired: not brought back
        assert await store.get("sid") is None

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
class _PlainStore:
    """A custom store with only the four ``SessionStore`` operations (no ``replace``)."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get(self, session_id: str) -> dict[str, Any] | None:
        return self._data.get(session_id)

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        self._data[session_id] = dict(data)

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    async def exists(self, session_id: str) -> bool:
        return session_id in self._data


class _CopyingStore(InMemorySessionStore):
    """The in-memory store, keeping a copy of what it saves as a store over a network does (the in-memory store
    keeps the session's own dict, so a later change shows through without a save)."""

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        await super().save(session_id, dict(data), ttl)


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

    @pytest.mark.asyncio
    async def test_a_change_after_persist_session_is_saved_when_the_request_ends(self) -> None:
        store = _CopyingStore()
        await store.save("existing", {"user": "ada"}, ttl=60)
        f = SessionFilter(store=store)
        request = _request(cookies={"PYFLY_SESSION": "existing"})

        async def call_next(req: Any) -> _Response:
            req.state.session.set_attribute("step", 1)
            await req.state.persist_session()
            assert await store.get("existing") == {**req.state.session.get_data(), "step": 1}
            req.state.session.set_attribute("step", 2)
            return _Response()

        await f.do_filter(request, call_next)
        assert (await store.get("existing"))["step"] == 2

    @pytest.mark.asyncio
    async def test_a_changed_session_ended_during_the_request_is_not_saved_back(self) -> None:
        """A logout or an eviction deleted the session while one of its requests was changing it: the request's
        persist saved it back (an upsert), and the revoked session was live again, its cookie sent anew."""
        store = InMemorySessionStore()
        await store.save("s1", {"user": "ada"}, ttl=60)
        f = SessionFilter(store=store)
        request = _request(cookies={"PYFLY_SESSION": "s1"})
        response = _Response()

        async def call_next(req: Any) -> _Response:
            req.state.session.set_attribute("cart", ["book"])
            await store.delete("s1")  # logged out or evicted meanwhile
            return response

        await f.do_filter(request, call_next)
        assert await store.get("s1") is None
        assert response.set_cookie_calls == []
        assert response.deleted == []  # no deletion either: the browser may hold a newer cookie by now

    @pytest.mark.asyncio
    async def test_a_store_without_replace_keeps_saving_changed_sessions(self) -> None:
        """A custom store implementing only the four ``SessionStore`` operations: a changed session is saved
        (an upsert), as before; such a store cannot tell a revoked session from a live one."""
        store = _PlainStore()
        await store.save("s1", {"user": "ada"}, ttl=60)
        f = SessionFilter(store=store)

        async def call_next(req: Any) -> _Response:
            req.state.session.set_attribute("step", 1)
            return _Response()

        await f.do_filter(_request(cookies={"PYFLY_SESSION": "s1"}), call_next)
        assert (await store.get("s1"))["step"] == 1

    @pytest.mark.asyncio
    async def test_a_session_ended_after_persist_session_is_not_saved_again(self) -> None:
        """The OAuth2 login saves the session before registering it; a concurrent login of the same principal
        can evict it (delete it from the store) before the login's request ends. The filter saved it again
        then, and the evicted session was back, live and no longer counted by the cap."""
        store = _CopyingStore()
        await store.save("pre-auth", {"oauth2_state": "s"}, ttl=60)
        f = SessionFilter(store=store)
        request = _request(cookies={"PYFLY_SESSION": "pre-auth"})

        async def call_next(req: Any) -> _Response:
            session = req.state.session
            session.rotate_id()
            session.set_attribute("user", "ada")
            await req.state.persist_session()
            assert await store.exists(session.id)
            await store.delete(session.id)  # evicted by a concurrent login
            return _Response()

        await f.do_filter(request, call_next)
        assert not await store.exists(request.state.session.id)
        assert not await store.exists("pre-auth")


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
