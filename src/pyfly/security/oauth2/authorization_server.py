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
"""OAuth2 Authorization Server — token endpoint with JWT issuance."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import jwt as pyjwt

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.oauth2.client import ClientRegistration, ClientRegistrationRepository


def _s256(verifier: str) -> str:
    """PKCE S256 transform: base64url(SHA-256(verifier)), no padding (RFC 7636)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Token Store ports and in-memory adapter
# ---------------------------------------------------------------------------

REFRESH_TOKEN = "refresh_token"
"""Record kind: an opaque refresh token."""

AUTHORIZATION_CODE = "authorization_code"
"""Record kind: an authorization code (RFC 6749 section 4.1)."""

PUSHED_REQUEST = "pushed_request"
"""Record kind: a pushed authorization request, keyed by its ``request_uri`` (RFC 9126)."""


class TokenStore(Protocol):
    """Key-value port for storing and retrieving OAuth2 tokens.

    The server keeps each record as a dict under a key. A store implementing only this port still works: the
    server runs its grants through a :class:`KeyValueTokenStore` over it, which makes them atomic within one
    process only. A store shared by several processes implements :class:`AtomicTokenStore`.
    """

    async def store(self, token_id: str, token_data: dict[str, Any]) -> None: ...

    async def find(self, token_id: str) -> dict[str, Any] | None: ...

    async def revoke(self, token_id: str) -> None: ...


class GrantOutcome(Enum):
    """What an atomic grant operation of an :class:`AtomicTokenStore` did."""

    GRANTED = "granted"
    """The code or refresh token was consumed and the new refresh token stored."""

    REPLAYED = "replayed"
    """The code or refresh token had been consumed already: the family it issued (or belongs to) is revoked."""

    REVOKED = "revoked"
    """The refresh token's family is revoked: nothing was changed."""

    EXPIRED = "expired"
    """The code or refresh token has expired: nothing was changed."""

    UNKNOWN = "unknown"
    """No such code or refresh token: nothing was changed."""


@dataclass(frozen=True)
class TokenRecord:
    """A refresh token, an authorization code or a pushed authorization request, as a token store keeps it.

    Attributes:
        token_id: The token, the code or the ``request_uri``.
        kind: :data:`REFRESH_TOKEN`, :data:`AUTHORIZATION_CODE` or :data:`PUSHED_REQUEST`.
        client_id: The client it was issued to.
        expires_at: When it expires, in seconds since the epoch (it is valid up to that second included).
        data: The rest of the record: the scope, and for a code the user, the redirect URI, the PKCE
            challenge and the nonce, for a pushed request its ``params``.
        family_id: A refresh token's rotation family; for a redeemed code, the family it issued.
        used: Whether the code or the refresh token was consumed.
        family_active: On a record a store loads, whether its family is still active.
    """

    token_id: str
    kind: str
    client_id: str
    expires_at: int
    data: dict[str, Any] = field(default_factory=dict)
    family_id: str | None = None
    used: bool = False
    family_active: bool = True

    def expired(self, now: int) -> bool:
        """Whether the record has expired at *now* (seconds since the epoch)."""
        return self.expires_at < now


@runtime_checkable
class AtomicTokenStore(Protocol):
    """The token-store port whose grant operations are atomic, across every process sharing the store.

    Each state change a grant makes is one operation here, which checks the state it depends on and changes
    it in one step (one unit of work of conditional statements on SQL, one script on Redis): a code or a
    refresh token is consumed once, however many requests present it at the same time, a rotation mints its
    token only while the family is active, and a revocation can never be undone by a rotation in flight. The
    server validates what never changes (the client, the redirect URI, the PKCE verifier) on a record it
    :meth:`load`-ed first, then calls the operation.
    """

    async def save(self, record: TokenRecord) -> None:
        """Store a new authorization code or pushed authorization request."""
        ...

    async def load(self, kind: str, token_id: str) -> TokenRecord | None:
        """The record of *kind* with *token_id* (compared exactly), with ``family_active`` set."""
        ...

    async def issue(self, token: TokenRecord) -> None:
        """Store refresh token *token*, opening its new family ``token.family_id``."""
        ...

    async def redeem(self, code: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        """Consume authorization code *code* and issue *token* in a new family, recorded on the code.

        Only an unused, unexpired code is consumed. A code consumed already revokes the family it issued
        (:attr:`GrantOutcome.REPLAYED`).
        """
        ...

    async def rotate(self, token_id: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        """Consume refresh token *token_id* and issue *token* in its family ``token.family_id``.

        Only an unused, unexpired token of an active family is consumed, and *token* is stored only while the
        family is active. A token consumed already revokes its family (:attr:`GrantOutcome.REPLAYED`).
        """
        ...

    async def take(self, kind: str, token_id: str, *, client_id: str, now: int) -> TokenRecord | None:
        """Remove and return the unexpired record of *client_id* with *token_id*: a one-time pushed request."""
        ...

    async def revoke_family(self, family_id: str) -> None:
        """Revoke family *family_id* for good and delete its refresh tokens."""
        ...


class KeyValueTokenStore:
    """The :class:`AtomicTokenStore` operations over a key-value :class:`TokenStore`.

    Every operation holds one lock of this object, so the grants of one process never interleave; the
    records keep the layout the server always wrote (a refresh token under its id, ``authcode:<code>``,
    ``par:<request_uri>`` and ``family:<id>``), so what a store holds from an earlier release stays valid.
    Nothing makes them atomic across processes, nor all-or-nothing when the store fails midway (the writes
    are ordered so that a failure leaves the code or token presented unconsumed): a store shared by several
    instances implements :class:`AtomicTokenStore` itself.
    """

    def __init__(self, delegate: TokenStore) -> None:
        self._delegate = delegate
        self._lock = asyncio.Lock()

    @property
    def delegate(self) -> TokenStore:
        """The key-value store the records live in."""
        return self._delegate

    # -- record layout ------------------------------------------------------------------------------------

    @staticmethod
    def _key(kind: str, token_id: str) -> str:
        if kind == AUTHORIZATION_CODE:
            return f"authcode:{token_id}"
        if kind == PUSHED_REQUEST:
            return f"par:{token_id}"
        return token_id

    @staticmethod
    def _family_key(family_id: str) -> str:
        return f"family:{family_id}"

    @staticmethod
    def _record(kind: str, token_id: str, stored: dict[str, Any], *, family_active: bool = True) -> TokenRecord:
        typed = ("client_id", "exp", "used", "family_id")
        return TokenRecord(
            token_id=token_id,
            kind=kind,
            client_id=str(stored.get("client_id") or ""),
            expires_at=int(stored.get("exp", 0)),
            data={name: value for name, value in stored.items() if name not in typed},
            family_id=stored.get("family_id"),
            used=bool(stored.get("used", False)),
            family_active=family_active,
        )

    @staticmethod
    def _stored(record: TokenRecord) -> dict[str, Any]:
        stored: dict[str, Any] = {**record.data, "client_id": record.client_id, "exp": record.expires_at}
        if record.kind != PUSHED_REQUEST:
            stored["used"] = record.used
        if record.family_id is not None:
            stored["family_id"] = record.family_id
        return stored

    async def _find(self, key: str) -> dict[str, Any] | None:
        found = await self._delegate.find(key)
        return dict(found) if found is not None else None

    async def _after_write(self) -> None:
        """Hook run after a write, outside the lock (the in-memory store purges there)."""

    # -- AtomicTokenStore ---------------------------------------------------------------------------------

    async def save(self, record: TokenRecord) -> None:
        async with self._lock:
            await self._delegate.store(self._key(record.kind, record.token_id), self._stored(record))
        await self._after_write()

    async def load(self, kind: str, token_id: str) -> TokenRecord | None:
        async with self._lock:
            stored = await self._find(self._key(kind, token_id))
            if stored is None:
                return None
            family_id = stored.get("family_id")
            family = await self._find(self._family_key(family_id)) if family_id else None
            # A family record that is gone counts as active, as it always did in this layout.
            active = family is None or bool(family.get("active", True))
            return self._record(kind, token_id, stored, family_active=active)

    async def issue(self, token: TokenRecord) -> None:
        async with self._lock:
            await self._open_family(token)
            await self._delegate.store(token.token_id, self._stored(token))
        await self._after_write()

    async def redeem(self, code: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        async with self._lock:
            key = self._key(AUTHORIZATION_CODE, code)
            stored = await self._find(key)
            if stored is None:
                return GrantOutcome.UNKNOWN
            if stored.get("used"):
                await self._revoke_issued_by(stored)
                return GrantOutcome.REPLAYED
            if int(stored.get("exp", 0)) < now:
                return GrantOutcome.EXPIRED
            # The code is marked last: a failure before leaves it redeemable.
            await self._open_family(token)
            await self._delegate.store(token.token_id, self._stored(token))
            await self._delegate.store(key, {**stored, "used": True, "family_id": token.family_id})
        await self._after_write()
        return GrantOutcome.GRANTED

    async def rotate(self, token_id: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        async with self._lock:
            stored = await self._find(token_id)
            if stored is None or stored.get("family_id") != token.family_id:
                return GrantOutcome.UNKNOWN
            family_key = self._family_key(str(token.family_id))
            family = await self._find(family_key)
            if family is not None and not family.get("active", True):
                return GrantOutcome.REVOKED
            if stored.get("used"):
                await self._revoke(str(token.family_id))
                return GrantOutcome.REPLAYED
            if int(stored.get("exp", 0)) < now:
                return GrantOutcome.EXPIRED
            # The presented token is marked last: a failure before leaves it redeemable. The family lists
            # its unused tokens only (a used one is refused through the family once it is revoked), so it
            # does not grow with every rotation.
            family = family or {"client_id": token.client_id, "active": True, "members": []}
            family["members"] = [member for member in family.get("members", []) if member != token_id]
            family["members"].append(token.token_id)
            family["exp"] = max(int(family.get("exp", 0)), token.expires_at)
            await self._delegate.store(token.token_id, self._stored(token))
            await self._delegate.store(family_key, family)
            await self._delegate.store(token_id, {**stored, "used": True})
        await self._after_write()
        return GrantOutcome.GRANTED

    async def take(self, kind: str, token_id: str, *, client_id: str, now: int) -> TokenRecord | None:
        async with self._lock:
            key = self._key(kind, token_id)
            stored = await self._find(key)
            if stored is None or stored.get("client_id") != client_id or int(stored.get("exp", 0)) < now:
                return None
            await self._delegate.revoke(key)
            return self._record(kind, token_id, stored)

    async def revoke_family(self, family_id: str) -> None:
        async with self._lock:
            await self._revoke(family_id)

    # -- internals (called with the lock held) ------------------------------------------------------------

    async def _open_family(self, token: TokenRecord) -> None:
        family = {"client_id": token.client_id, "active": True, "members": [token.token_id], "exp": token.expires_at}
        await self._delegate.store(self._family_key(str(token.family_id)), family)

    async def _revoke(self, family_id: str) -> None:
        family_key = self._family_key(family_id)
        family = await self._find(family_key)
        if family is None:
            return
        await self._delegate.store(family_key, {**family, "active": False})
        for member in family.get("members", []):
            await self._delegate.revoke(member)

    async def _revoke_issued_by(self, code: dict[str, Any]) -> None:
        family_id = code.get("family_id")
        if family_id is None and code.get("issued_refresh"):
            # A code redeemed by an earlier release names the refresh token it issued.
            issued = await self._find(str(code["issued_refresh"]))
            family_id = issued.get("family_id") if issued is not None else None
            if family_id is None:
                await self._delegate.revoke(str(code["issued_refresh"]))
        if family_id is not None:
            await self._revoke(str(family_id))


class InMemoryTokenStore(KeyValueTokenStore):
    """In-memory token store — suitable for development and testing (one process).

    Its records live in a dict (the :class:`TokenStore` operations are the dict's), and its grants are atomic
    within the process. Expired records are purged at most once per *purge_interval* after a write, once
    they expired more than *purge_grace* ago (the reuse-detection grace period), and by
    :meth:`purge_expired`.
    """

    def __init__(
        self,
        *,
        purge_interval: timedelta | None = timedelta(seconds=60),
        purge_grace: timedelta = timedelta(hours=1),
    ) -> None:
        super().__init__(self)
        self._tokens: dict[str, dict[str, Any]] = {}
        self._purge_interval = purge_interval.total_seconds() if purge_interval is not None else None
        self.purge_grace = purge_grace
        self._last_purge = time.monotonic()

    async def store(self, token_id: str, token_data: dict[str, Any]) -> None:
        self._tokens[token_id] = token_data

    async def find(self, token_id: str) -> dict[str, Any] | None:
        return self._tokens.get(token_id)

    async def revoke(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)

    async def purge_expired(self, *, before: int | None = None) -> int:
        """Delete every record (a family included) that expired before *before* (seconds since the epoch; by
        default now minus the grace period); return how many were deleted."""
        cutoff = before if before is not None else int(time.time() - self.purge_grace.total_seconds())
        async with self._lock:
            expired = [key for key, record in self._tokens.items() if "exp" in record and int(record["exp"]) < cutoff]
            for key in expired:
                del self._tokens[key]
        return len(expired)

    async def _after_write(self) -> None:
        interval = self._purge_interval
        if interval is None or time.monotonic() - self._last_purge < interval:
            return
        self._last_purge = time.monotonic()
        await self.purge_expired()


# ---------------------------------------------------------------------------
# Authorization Server
# ---------------------------------------------------------------------------


class AuthorizationServer:
    """OAuth2 Authorization Server — issues JWT access tokens.

    Supports grant types:
    - client_credentials: machine-to-machine authentication
    - refresh_token: exchange a refresh token for a new access token
    - authorization_code: redeem a code (PKCE mandatory)

    Every change a grant makes to its token store is one atomic operation of an :class:`AtomicTokenStore`: a
    code or refresh token is consumed once however many requests present it, a replay revokes the family it
    issued, a rotation never undoes a revocation, and a grant that fails midway changes nothing. A store with
    only the key-value :class:`TokenStore` operations is run through a :class:`KeyValueTokenStore`, atomic
    within this process only.

    Args:
        secret: Secret key for HMAC signing (used when ``algorithm`` is ``HS*``).
        client_repository: Repository to look up client registrations.
        token_store: Store for refresh tokens, authorization codes, rotation families and pushed requests:
            an :class:`AtomicTokenStore`, or a key-value :class:`TokenStore`.
        access_token_ttl: Access token lifetime in seconds (default: 3600 = 1 hour).
        refresh_token_ttl: Refresh token lifetime in seconds (default: 86400 = 24 hours).
        issuer: Token issuer claim (optional).
        audience: Audience the issued tokens are restricted to (``aud`` claim).
            Accepts a single value or a list. When unset, no ``aud`` is emitted
            (backward compatible). Setting it lets resource servers reject tokens
            minted for a different API (RFC 9700 / OAuth 2.1 audience restriction).
        algorithm: JWS algorithm. ``HS256`` (default) signs with ``secret``;
            ``RS256``/``RS384``/``RS512``/``PS*``/``ES256``/``ES384``/``ES512``
            sign with ``private_key`` and publish the matching public key via
            :meth:`jwks`, so a resource server can verify AS-minted tokens.
        private_key: PEM string/bytes or a cryptography private-key object, required
            for asymmetric algorithms.
        key_id: ``kid`` placed in the JWT header and the published JWK.
    """

    def __init__(
        self,
        secret: str,
        client_repository: ClientRegistrationRepository,
        token_store: TokenStore | AtomicTokenStore,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 86400,
        issuer: str | None = None,
        audience: str | list[str] | None = None,
        algorithm: str = "HS256",
        private_key: Any = None,
        key_id: str | None = None,
        allow_dynamic_registration: bool = False,
        registration_access_token: str | None = None,
        auth_code_ttl: int = 60,
    ) -> None:
        self._secret = secret
        self.allow_dynamic_registration = allow_dynamic_registration
        self.registration_access_token = registration_access_token
        self._auth_code_ttl = auth_code_ttl
        self._client_repository = client_repository
        self._token_store = token_store
        self._grants: AtomicTokenStore = (
            token_store if isinstance(token_store, AtomicTokenStore) else KeyValueTokenStore(token_store)
        )
        self._access_token_ttl = access_token_ttl
        self._refresh_token_ttl = refresh_token_ttl
        self._issuer = issuer
        self._algorithm = algorithm.upper()
        self._is_asymmetric = self._algorithm[:2] in ("RS", "ES", "PS")
        self._key_id = key_id
        self._private_key: Any = self._coerce_private_key(private_key) if self._is_asymmetric else None
        if self._is_asymmetric and self._private_key is None:
            raise ValueError(f"algorithm {self._algorithm} requires a private_key")
        if audience is None:
            self._audience: str | list[str] | None = None
        elif isinstance(audience, str):
            self._audience = audience
        else:
            aud_list = [a for a in audience if a]
            self._audience = aud_list or None

    @property
    def issuer(self) -> str | None:
        """The configured issuer identifier, if any."""
        return self._issuer

    @property
    def token_store(self) -> TokenStore | AtomicTokenStore:
        """The token store the server was given."""
        return self._token_store

    async def start(self) -> None:
        """Start the token store (a SQL store creates or checks its tables): a lifecycle bean's start."""
        start = getattr(self._token_store, "start", None)
        if callable(start):
            await start()

    async def stop(self) -> None:
        """Stop the token store (idempotent)."""
        stop = getattr(self._token_store, "stop", None)
        if callable(stop):
            await stop()

    @property
    def signing_algorithm(self) -> str:
        """The JWS algorithm used to sign tokens (e.g. ``HS256``/``RS256``)."""
        return self._algorithm

    @staticmethod
    def _coerce_private_key(private_key: Any) -> Any:
        """Load a PEM string/bytes into a key object; pass through key objects."""
        if isinstance(private_key, (str, bytes)):
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            data = private_key.encode("utf-8") if isinstance(private_key, str) else private_key
            return load_pem_private_key(data, password=None)
        return private_key

    def _encode(self, payload: dict[str, Any]) -> str:
        """Sign *payload* with the configured algorithm (HMAC or asymmetric+kid)."""
        if self._is_asymmetric:
            assert self._private_key is not None  # guaranteed by __init__
            headers = {"kid": self._key_id} if self._key_id else None
            return pyjwt.encode(payload, self._private_key, algorithm=self._algorithm, headers=headers)
        return pyjwt.encode(payload, self._secret, algorithm=self._algorithm)

    def jwks(self) -> dict[str, Any]:
        """Return the public JWK Set for token verification (empty for HMAC)."""
        if not self._is_asymmetric or self._private_key is None:
            return {"keys": []}
        import json as _json

        assert self._private_key is not None  # narrowed for mypy
        public_key = self._private_key.public_key()
        if self._algorithm[:2] == "ES":
            jwk = _json.loads(pyjwt.algorithms.ECAlgorithm.to_jwk(public_key))
        else:
            jwk = _json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key))
        jwk.update({"use": "sig", "alg": self._algorithm})
        if self._key_id:
            jwk["kid"] = self._key_id
        return {"keys": [jwk]}

    async def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Register a new client dynamically (RFC 7591) and return its metadata.

        Requires ``allow_dynamic_registration`` and a client repository that
        supports ``add``. Generates the ``client_id`` / ``client_secret``; the
        endpoint layer enforces any initial access token.
        """
        if not self.allow_dynamic_registration:
            raise SecurityException("Dynamic client registration is disabled", code="REGISTRATION_DISABLED")
        repo = self._client_repository
        add = getattr(repo, "add", None)
        if not callable(add):
            raise SecurityException(
                "The configured client repository does not support registration", code="REGISTRATION_UNSUPPORTED"
            )
        grant_types = metadata.get("grant_types") or ["client_credentials"]
        redirect_uris = metadata.get("redirect_uris") or []
        scope = metadata.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) else list(scope or [])
        client_id = secrets.token_urlsafe(16)
        client_secret = secrets.token_urlsafe(32)
        registration = ClientRegistration(
            registration_id=client_id,
            client_id=client_id,
            client_secret=client_secret,
            authorization_grant_type=str(grant_types[0]),
            redirect_uri=str(redirect_uris[0]) if redirect_uris else "",
            scopes=scopes,
            provider_name=str(metadata.get("client_name", "")),
        )
        add(registration)
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,  # never expires
            "grant_types": list(grant_types),
            "redirect_uris": list(redirect_uris),
            "scope": " ".join(scopes),
            "token_endpoint_auth_method": "client_secret_basic",
            "client_name": str(metadata.get("client_name", "")),
        }

    def authenticate_client(self, client_id: str, client_secret: str) -> ClientRegistration | None:
        """Return the registration iff *client_id*/*client_secret* match (constant time).

        Client authentication requires real credentials: an empty client id or
        secret — or a registration that has no secret configured — never
        authenticates (prevents an empty-credential bypass on the management
        endpoints and for any client that is not a confidential client).
        """
        if not client_id or not client_secret:
            return None
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None or not registration.client_secret:
            return None
        if not secrets.compare_digest(registration.client_secret.encode("utf-8"), client_secret.encode("utf-8")):
            return None
        return registration

    def _verification_key(self) -> Any:
        return self._private_key.public_key() if self._is_asymmetric else self._secret

    async def introspect(
        self, token: str, *, requesting_client_id: str | None = None, allow_any_client: bool = False
    ) -> dict[str, Any]:
        """RFC 7662 token introspection for an access (JWT) or refresh token.

        When *requesting_client_id* is given and *allow_any_client* is False, a
        token owned by a different client is reported as inactive — so a client
        cannot scan another client's tokens (information disclosure). Designated
        resource-server clients pass ``allow_any_client=True``.
        """
        result = await self._introspect(token)
        if (
            result.get("active")
            and requesting_client_id is not None
            and not allow_any_client
            and result.get("client_id") != requesting_client_id
        ):
            return {"active": False}
        return result

    async def _introspect(self, token: str) -> dict[str, Any]:
        # Access token: a self-contained, signature-verified JWT.
        try:
            payload = pyjwt.decode(
                token,
                self._verification_key(),
                algorithms=[self._algorithm],
                options={"require": ["exp"], "verify_aud": False},
            )
            active: dict[str, Any] = {"active": True, "token_type": "Bearer"}
            for claim in ("sub", "scope", "iat", "exp", "iss", "aud"):
                if claim in payload:
                    active[claim] = payload[claim]
            active.setdefault("client_id", payload.get("sub"))
            return active
        except pyjwt.PyJWTError:
            pass

        # Refresh token: opaque, looked up in the store; active iff present,
        # unused, unexpired, and its family is still active.
        record = await self._grants.load(REFRESH_TOKEN, token)
        if record is None or record.used or record.expired(int(time.time())) or not record.family_active:
            return {"active": False}
        return {
            "active": True,
            "token_type": "refresh_token",
            "client_id": record.client_id,
            "scope": record.data.get("scope", ""),
            "exp": record.expires_at,
        }

    async def token(
        self,
        grant_type: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        refresh_token: str | None = None,
        confirmation: dict[str, Any] | None = None,
        code: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
    ) -> dict[str, Any]:
        """Issue tokens based on grant type.

        Args:
            grant_type: "client_credentials", "refresh_token" or "authorization_code"
            client_id: The client's ID
            client_secret: The client's secret (confidential clients)
            scope: Space-separated scopes (for client_credentials)
            refresh_token: The refresh token (for refresh_token grant)
            confirmation: Optional ``cnf`` confirmation claim to bind the access
                token to a key (e.g. ``{"jkt": ...}`` for DPoP, ``{"x5t#S256": ...}``
                for mTLS) — sender-constraining per RFC 9449 / RFC 8705.
            code: Authorization code (for authorization_code grant).
            redirect_uri: Redirect URI used in the authorization request (must match).
            code_verifier: PKCE verifier (for authorization_code grant).

        Returns:
            Token response dict with access_token, token_type, expires_in,
            and optionally refresh_token / id_token.

        Raises:
            SecurityException: If authentication fails or grant type is unsupported.
        """
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None:
            raise SecurityException("Invalid client credentials", code="INVALID_CLIENT")

        # Client authentication: a confidential client (one with a registered
        # secret) MUST present it. A public client (no secret) is permitted only
        # for the authorization_code grant, where PKCE provides proof of possession.
        is_public = not registration.client_secret
        if not is_public:
            if self.authenticate_client(client_id, client_secret) is None:
                raise SecurityException("Invalid client credentials", code="INVALID_CLIENT")
        elif grant_type != "authorization_code":
            raise SecurityException("Public clients may not use this grant", code="INVALID_CLIENT")

        if grant_type == "client_credentials":
            # The client must be registered for the client_credentials grant to
            # mint a client_credentials token — prevents grant-type confusion (a
            # client registered only for authorization_code must not use it).
            if registration.authorization_grant_type != "client_credentials":
                raise SecurityException(
                    f"Client '{client_id}' is not authorized for grant type 'client_credentials'",
                    code="UNAUTHORIZED_CLIENT",
                )
            return await self._handle_client_credentials(registration, scope, confirmation)
        elif grant_type == "refresh_token":
            if refresh_token is None:
                raise SecurityException("Refresh token required", code="INVALID_REQUEST")
            return await self._handle_refresh_token(registration, refresh_token, confirmation)
        elif grant_type == "authorization_code":
            if code is None:
                raise SecurityException("Authorization code required", code="INVALID_REQUEST")
            return await self._handle_authorization_code(registration, code, redirect_uri, code_verifier, confirmation)
        else:
            raise SecurityException(
                f"Unsupported grant type: {grant_type}",
                code="UNSUPPORTED_GRANT_TYPE",
            )

    # ------------------------------------------------------------------
    # Authorization Code grant (RFC 6749 §4.1 + PKCE RFC 7636 + OIDC)
    # ------------------------------------------------------------------

    async def authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        user_id: str,
        response_type: str = "code",
        scope: str = "",
        state: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str = "S256",
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Process an authorization request and issue a single-use authorization code.

        *user_id* is the already-authenticated resource owner. Enforces exact
        redirect-URI matching, scope subset, and **mandatory PKCE (S256)** (OAuth
        2.1 / RFC 9700). Returns ``{code, redirect_uri[, state][, iss]}``.

        Raises:
            SecurityException: ``INVALID_CLIENT`` / ``INVALID_REDIRECT_URI`` must NOT
            be redirected back to the client; other codes are safe to surface to the
            client via a redirect ``error`` parameter.
        """
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None:
            raise SecurityException("Unknown client", code="INVALID_CLIENT")
        # Exact redirect-URI match (RFC 9700 / OAuth 2.1) — never redirect on mismatch.
        if redirect_uri != registration.redirect_uri:
            raise SecurityException("redirect_uri does not match the registration", code="INVALID_REDIRECT_URI")
        if response_type != "code":
            raise SecurityException(f"Unsupported response_type: {response_type}", code="UNSUPPORTED_RESPONSE_TYPE")

        requested = scope.split() if scope else list(registration.scopes)
        unregistered = [s for s in requested if s not in registration.scopes]
        if unregistered:
            raise SecurityException(f"Unpermitted scope(s): {' '.join(unregistered)}", code="INVALID_SCOPE")

        # PKCE is mandatory and must be S256 (OAuth 2.1 §4.1.1 / RFC 7636).
        if not code_challenge:
            raise SecurityException("PKCE code_challenge is required", code="INVALID_REQUEST")
        if code_challenge_method != "S256":
            raise SecurityException("Only the S256 PKCE method is supported", code="INVALID_REQUEST")

        code = secrets.token_urlsafe(32)
        await self._grants.save(
            TokenRecord(
                token_id=code,
                kind=AUTHORIZATION_CODE,
                client_id=registration.client_id,
                expires_at=int(time.time()) + self._auth_code_ttl,
                data={
                    "redirect_uri": redirect_uri,
                    "scope": " ".join(requested),
                    "code_challenge": code_challenge,
                    "user_id": user_id,
                    "nonce": nonce,
                },
            )
        )
        result: dict[str, Any] = {"code": code, "redirect_uri": redirect_uri}
        if state is not None:
            result["state"] = state
        if self._issuer:
            result["iss"] = self._issuer  # RFC 9207 mix-up defense
        return result

    # ------------------------------------------------------------------
    # Pushed Authorization Requests (RFC 9126) + request objects (RFC 9101)
    # ------------------------------------------------------------------

    async def pushed_authorization_request(self, client_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Store a pushed authorization request and return its ``request_uri`` (RFC 9126)."""
        request_uri = "urn:ietf:params:oauth:request_uri:" + secrets.token_urlsafe(24)
        await self._grants.save(
            TokenRecord(
                token_id=request_uri,
                kind=PUSHED_REQUEST,
                client_id=client_id,
                expires_at=int(time.time()) + 90,
                data={"params": dict(params)},
            )
        )
        return {"request_uri": request_uri, "expires_in": 90}

    async def consume_pushed_request(self, request_uri: str, client_id: str) -> dict[str, Any] | None:
        """Return (and one-time consume) the params for *request_uri* if valid for *client_id*.

        The store removes the request atomically: of concurrent requests presenting one ``request_uri``, one
        gets the params.
        """
        record = await self._grants.take(PUSHED_REQUEST, request_uri, client_id=client_id, now=int(time.time()))
        if record is None:
            return None
        return dict(record.data.get("params") or {})

    def verify_request_object(self, client_id: str, request_jwt: str) -> dict[str, Any]:
        """Verify a JAR request object (RFC 9101) signed with the client secret (HS256)."""
        registration = self._client_repository.find_by_registration_id(client_id)
        if registration is None or not registration.client_secret:
            raise SecurityException("Request objects require a confidential client", code="INVALID_REQUEST")
        try:
            claims: dict[str, Any] = pyjwt.decode(
                request_jwt, registration.client_secret, algorithms=["HS256"], options={"verify_aud": False}
            )
        except pyjwt.PyJWTError as exc:
            raise SecurityException(f"Invalid request object: {exc}", code="INVALID_REQUEST") from exc
        return claims

    async def _handle_authorization_code(
        self,
        registration: ClientRegistration,
        code: str,
        redirect_uri: str | None,
        code_verifier: str | None,
        confirmation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record = await self._grants.load(AUTHORIZATION_CODE, code)
        if record is None:
            raise SecurityException("Invalid authorization code", code="INVALID_GRANT")

        # Single-use: a replayed code is treated as injection — revoke the tokens
        # already issued from it (RFC 9700) and reject.
        if record.used:
            if record.family_id is not None:
                await self._grants.revoke_family(record.family_id)
            else:
                await self._revoke_issued_by_earlier_release(record)
            raise SecurityException("Authorization code already used", code="INVALID_GRANT")
        if record.client_id != registration.client_id:
            raise SecurityException("Authorization code was issued to another client", code="INVALID_GRANT")
        if redirect_uri is not None and record.data.get("redirect_uri") != redirect_uri:
            raise SecurityException("redirect_uri mismatch", code="INVALID_GRANT")
        now = int(time.time())
        if record.expired(now):
            raise SecurityException("Authorization code expired", code="INVALID_GRANT")

        # PKCE verification (mandatory).
        challenge = record.data.get("code_challenge")
        if not code_verifier or _s256(code_verifier) != challenge:
            raise SecurityException("PKCE verification failed", code="INVALID_GRANT")

        scope = str(record.data.get("scope", ""))
        user_id = record.data.get("user_id")

        access_payload: dict[str, Any] = {
            "sub": user_id,
            "scope": scope,
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer
        if self._audience is not None:
            access_payload["aud"] = self._audience
        if confirmation:
            access_payload["cnf"] = confirmation
        access_token = self._encode(access_payload)

        # Consume the code and issue the refresh token (a new family, recorded on the
        # code so a replay can revoke it) in one atomic step: of concurrent redemptions
        # one succeeds, and the others revoke what it issued (RFC 6749 §4.1.2).
        refresh = self._new_refresh_token(registration.client_id, scope, family_id=secrets.token_urlsafe(16))
        outcome = await self._grants.redeem(code, refresh, now=now)
        if outcome is GrantOutcome.REPLAYED:
            raise SecurityException("Authorization code already used", code="INVALID_GRANT")
        if outcome is GrantOutcome.EXPIRED:
            raise SecurityException("Authorization code expired", code="INVALID_GRANT")
        if outcome is not GrantOutcome.GRANTED:
            raise SecurityException("Invalid authorization code", code="INVALID_GRANT")

        response: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": refresh.token_id,
            "scope": scope,
        }
        if "openid" in scope.split():
            response["id_token"] = self._issue_id_token(str(user_id), registration.client_id, record.data.get("nonce"))
        return response

    async def _revoke_issued_by_earlier_release(self, code: TokenRecord) -> None:
        """A code redeemed by an earlier release names the refresh token it issued (``issued_refresh``)."""
        issued = code.data.get("issued_refresh")
        if not issued:
            return
        token = await self._grants.load(REFRESH_TOKEN, str(issued))
        if token is not None and token.family_id is not None:
            await self._grants.revoke_family(token.family_id)

    def _issue_id_token(self, subject: str, client_id: str, nonce: str | None) -> str:
        """Issue an OIDC ID token (aud = client_id) for the openid scope."""
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": subject,
            "aud": client_id,
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            payload["iss"] = self._issuer
        if nonce:
            payload["nonce"] = nonce
        return self._encode(payload)

    async def _handle_client_credentials(
        self, registration: ClientRegistration, scope: str, confirmation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = int(time.time())
        # A client may only ever obtain scopes it is registered for. Requesting an
        # unregistered scope is rejected wholesale (RFC 6749 §5.2 ``invalid_scope``)
        # rather than silently echoed — otherwise any authenticated client could
        # mint an arbitrarily-privileged token (e.g. ``admin``) just by asking.
        if scope:
            requested = scope.split()
            unregistered = [s for s in requested if s not in registration.scopes]
            if unregistered:
                raise SecurityException(
                    f"Requested scope(s) not permitted for this client: {' '.join(unregistered)}",
                    code="INVALID_SCOPE",
                )
            scopes = requested
        else:
            scopes = registration.scopes

        access_payload: dict[str, Any] = {
            "sub": registration.client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer
        if self._audience is not None:
            access_payload["aud"] = self._audience
        if confirmation:
            access_payload["cnf"] = confirmation

        access_token = self._encode(access_payload)

        scope_str = " ".join(scopes)
        refresh = self._new_refresh_token(registration.client_id, scope_str, family_id=secrets.token_urlsafe(16))
        await self._grants.issue(refresh)
        refresh_token_id = refresh.token_id

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": refresh_token_id,
            "scope": scope_str,
        }

    async def _handle_refresh_token(
        self, registration: ClientRegistration, refresh_token: str, confirmation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        record = await self._grants.load(REFRESH_TOKEN, refresh_token)
        if record is None:
            raise SecurityException("Invalid refresh token", code="INVALID_GRANT")

        # The family was already revoked (e.g. by a previous reuse) — refuse.
        if not record.family_active:
            raise SecurityException("Refresh token family revoked", code="INVALID_GRANT")

        # Reuse detection (OAuth 2.1 / RFC 9700): a refresh token that was already
        # rotated is being replayed. The legitimate holder cannot do this, so we
        # treat it as theft and revoke the entire token family.
        if record.used:
            if record.family_id is not None:
                await self._grants.revoke_family(record.family_id)
            raise SecurityException("Refresh token reuse detected", code="INVALID_GRANT")

        # Verify client matches
        if record.client_id != registration.client_id:
            raise SecurityException("Refresh token client mismatch", code="INVALID_GRANT")

        # Check expiration
        now = int(time.time())
        if record.expired(now):
            raise SecurityException("Refresh token expired", code="INVALID_GRANT")
        if record.family_id is None:  # every refresh token is minted in a family
            raise SecurityException("Invalid refresh token", code="INVALID_GRANT")

        scope = str(record.data.get("scope", ""))

        # Rotate: consume the presented token (it is retained, so a later replay is
        # detected as reuse rather than "unknown") and mint its successor in the same
        # family, in one atomic step. Of concurrent rotations one succeeds; the others
        # are replays and revoke the family.
        successor = self._new_refresh_token(registration.client_id, scope, family_id=record.family_id)
        outcome = await self._grants.rotate(refresh_token, successor, now=now)
        if outcome is GrantOutcome.REPLAYED:
            raise SecurityException("Refresh token reuse detected", code="INVALID_GRANT")
        if outcome is GrantOutcome.REVOKED:
            raise SecurityException("Refresh token family revoked", code="INVALID_GRANT")
        if outcome is GrantOutcome.EXPIRED:
            raise SecurityException("Refresh token expired", code="INVALID_GRANT")
        if outcome is not GrantOutcome.GRANTED:
            raise SecurityException("Invalid refresh token", code="INVALID_GRANT")

        # Issue new tokens
        access_payload: dict[str, Any] = {
            "sub": registration.client_id,
            "scope": scope,
            "iat": now,
            "exp": now + self._access_token_ttl,
        }
        if self._issuer:
            access_payload["iss"] = self._issuer
        if self._audience is not None:
            access_payload["aud"] = self._audience
        if confirmation:
            access_payload["cnf"] = confirmation

        access_token = self._encode(access_payload)

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._access_token_ttl,
            "refresh_token": successor.token_id,
            "scope": scope,
        }

    # ------------------------------------------------------------------
    # Refresh tokens and revocation
    # ------------------------------------------------------------------

    def _new_refresh_token(self, client_id: str, scope: str, *, family_id: str) -> TokenRecord:
        """A new refresh token of *family_id*, valid for the refresh-token lifetime."""
        return TokenRecord(
            token_id=secrets.token_urlsafe(32),
            kind=REFRESH_TOKEN,
            client_id=client_id,
            expires_at=int(time.time()) + self._refresh_token_ttl,
            data={"scope": scope},
            family_id=family_id,
        )

    async def revoke(self, token_id: str, *, requesting_client_id: str | None = None) -> None:
        """Revoke a refresh token and its whole rotation family.

        Per RFC 7009 §2.1, when *requesting_client_id* is given the token is only
        revoked if it was issued to that client — a client cannot revoke another
        client's tokens. ``requesting_client_id=None`` (internal callers) revokes
        unconditionally. The revocation is final: a rotation in flight never
        brings the family back.
        """
        record = await self._grants.load(REFRESH_TOKEN, token_id)
        if record is None:
            return
        if requesting_client_id is not None and record.client_id and record.client_id != requesting_client_id:
            return  # not the owner — refuse silently (RFC 7009 still returns 200)
        if record.family_id is not None:
            await self._grants.revoke_family(record.family_id)
