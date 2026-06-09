# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Internal-DB IDP adapter — bcrypt-hashed password store; works out of the box."""

from __future__ import annotations

import asyncio
import secrets
import uuid

from pyfly.idp.models import (
    AuthResult,
    IdpRole,
    IdpUser,
    LoginRequest,
    MfaChallenge,
    PasswordChangeRequest,
    SessionIntrospection,
)


class InternalDbIdpAdapter:
    """Reference adapter — stores users in memory, hashes passwords with bcrypt.

    Useful for development, tests, and small services that don't need a
    full external IDP.
    """

    name = "internal-db"

    def __init__(self) -> None:
        self._users: dict[str, IdpUser] = {}
        self._passwords: dict[str, bytes] = {}
        self._tokens: dict[str, str] = {}  # token → user_id
        self._refresh: dict[str, str] = {}  # refresh_token → user_id
        self._roles: dict[str, IdpRole] = {}
        self._mfa_secrets: dict[str, str] = {}  # user_id → TOTP secret
        self._mfa_challenges: dict[str, str] = {}  # challenge_id → user_id
        self._lock = asyncio.Lock()

    # -- User management ---------------------------------------------------

    async def create_user(self, user: IdpUser, password: str) -> IdpUser:
        async with self._lock:
            self._users[user.id] = user
            self._passwords[user.id] = self._hash(password)
        return user

    async def get_user(self, user_id: str) -> IdpUser | None:
        async with self._lock:
            return self._users.get(user_id)

    async def find_by_username(self, username: str) -> IdpUser | None:
        async with self._lock:
            for u in self._users.values():
                if u.username == username:
                    return u
        return None

    async def update_user(self, user: IdpUser) -> IdpUser:
        async with self._lock:
            self._users[user.id] = user
        return user

    async def delete_user(self, user_id: str) -> bool:
        async with self._lock:
            self._passwords.pop(user_id, None)
            self._mfa_secrets.pop(user_id, None)
            return self._users.pop(user_id, None) is not None

    async def list_users(self, *, limit: int = 100) -> list[IdpUser]:
        async with self._lock:
            return list(self._users.values())[:limit]

    # -- Authentication ----------------------------------------------------

    async def login(self, request: LoginRequest) -> AuthResult:
        user = await self.find_by_username(request.username)
        if user is None or not user.enabled:
            msg = "invalid credentials"
            raise PermissionError(msg)
        if not self._verify(request.password, self._passwords[user.id]):
            msg = "invalid credentials"
            raise PermissionError(msg)

        # MFA gate: if MFA is enabled for this user, either verify the supplied
        # code or return a challenge (no tokens issued until verified).
        async with self._lock:
            mfa_secret = self._mfa_secrets.get(user.id)

        if mfa_secret is not None:
            if request.mfa_code is not None:
                # Verify inline — raises on failure.
                self._verify_totp(mfa_secret, request.mfa_code)
            else:
                # No code supplied → issue a challenge, return without tokens.
                challenge = await self.mfa_challenge(user.id)
                return AuthResult(
                    user=user,
                    access_token="",
                    mfa_required=True,
                    mfa_challenge=challenge,
                )

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        async with self._lock:
            self._tokens[access] = user.id
            self._refresh[refresh] = user.id
        return AuthResult(user=user, access_token=access, refresh_token=refresh)

    async def logout(self, access_token: str) -> bool:
        async with self._lock:
            return self._tokens.pop(access_token, None) is not None

    async def refresh(self, refresh_token: str) -> AuthResult:
        async with self._lock:
            user_id = self._refresh.get(refresh_token)
        if user_id is None:
            msg = "invalid refresh token"
            raise PermissionError(msg)
        user = await self.get_user(user_id)
        assert user is not None
        access = secrets.token_urlsafe(32)
        async with self._lock:
            self._tokens[access] = user_id
        return AuthResult(user=user, access_token=access, refresh_token=refresh_token)

    async def introspect(self, access_token: str) -> SessionIntrospection:
        async with self._lock:
            user_id = self._tokens.get(access_token)
        if user_id is None:
            return SessionIntrospection(active=False)
        user = await self.get_user(user_id)
        if user is None:
            return SessionIntrospection(active=False)
        return SessionIntrospection(
            active=True,
            user_id=user.id,
            username=user.username,
            scopes=list(user.roles),
        )

    # -- Password / MFA ----------------------------------------------------

    async def change_password(self, request: PasswordChangeRequest) -> bool:
        async with self._lock:
            current = self._passwords.get(request.user_id)
            if current is None or not self._verify(request.old_password, current):
                return False
            self._passwords[request.user_id] = self._hash(request.new_password)
        return True

    async def reset_password(self, user_id: str) -> str:
        new_password = secrets.token_urlsafe(16)
        async with self._lock:
            self._passwords[user_id] = self._hash(new_password)
        return new_password

    # -- MFA (Java parity) -------------------------------------------------

    async def enable_mfa(self, user_id: str) -> str:
        """Enable TOTP MFA for *user_id*.

        Returns the provisioning secret that should be displayed to the user
        (e.g. via a QR-code URI built with ``pyotp.TOTP(secret).provisioning_uri``).
        Raises ``KeyError`` if the user does not exist.
        """
        try:
            import pyotp  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            msg = "TOTP MFA requires pyotp — `pip install pyfly[security]`"
            raise ImportError(msg) from exc

        async with self._lock:
            if user_id not in self._users:
                msg = f"user {user_id!r} not found"
                raise KeyError(msg)
            secret: str = pyotp.random_base32()
            self._mfa_secrets[user_id] = secret
        return secret

    async def mfa_challenge(self, user_id: str) -> MfaChallenge:
        """Create a TOTP challenge for *user_id* (user must exist).

        A challenge_id → user_id mapping is stored so that :meth:`mfa_verify`
        can resolve the user without exposing the user_id in the public API.
        """
        async with self._lock:
            if user_id not in self._users:
                msg = f"user {user_id!r} not found"
                raise KeyError(msg)
            challenge_id = secrets.token_urlsafe(32)
            self._mfa_challenges[challenge_id] = user_id
        return MfaChallenge(challenge_id=challenge_id, user_id=user_id, method="TOTP")

    async def mfa_verify(self, challenge_id: str, code: str) -> AuthResult:
        """Verify a TOTP *code* against *challenge_id* and issue tokens on success.

        Raises :class:`PermissionError` on invalid challenge or wrong code.
        """
        async with self._lock:
            user_id = self._mfa_challenges.pop(challenge_id, None)
        if user_id is None:
            msg = "invalid or expired MFA challenge"
            raise PermissionError(msg)

        async with self._lock:
            secret = self._mfa_secrets.get(user_id)
        if secret is None:
            msg = "MFA not enabled for this user"
            raise PermissionError(msg)

        self._verify_totp(secret, code)

        user = await self.get_user(user_id)
        if user is None:
            msg = "user not found"
            raise PermissionError(msg)

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        async with self._lock:
            self._tokens[access] = user_id
            self._refresh[refresh] = user_id
        return AuthResult(user=user, access_token=access, refresh_token=refresh)

    # -- Extended user info (Java parity) ----------------------------------

    async def get_user_info(self, access_token: str) -> IdpUser | None:
        """Resolve an access token to the owning :class:`IdpUser`."""
        async with self._lock:
            user_id = self._tokens.get(access_token)
        if user_id is None:
            return None
        return await self.get_user(user_id)

    async def register_user(self, user: IdpUser, password: str) -> IdpUser:
        """Public self-registration — always sets *enabled=True*, no admin role.

        Delegates to :meth:`create_user` after enforcing registration defaults.
        """
        user.enabled = True
        # Ensure the user does not gain privileged roles via self-registration.
        user.roles = [r for r in user.roles if r != "admin"]
        return await self.create_user(user, password)

    async def get_roles(self, user_id: str) -> list[IdpRole]:
        """Return the roles assigned to *user_id* as :class:`IdpRole` objects."""
        async with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return []
            return [self._roles.get(role_name, IdpRole(name=role_name)) for role_name in user.roles]

    # -- Roles -------------------------------------------------------------

    async def assign_role(self, user_id: str, role: str) -> bool:
        async with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            if role not in user.roles:
                user.roles.append(role)
            # Populate the role catalogue so list_roles reflects assigned roles
            # (audit #29) — previously self._roles was never written.
            self._roles.setdefault(role, IdpRole(name=role))
        return True

    async def create_roles(self, *roles: str) -> list[IdpRole]:
        """Create named roles in the catalogue (audit #29)."""
        async with self._lock:
            created = []
            for role in roles:
                idp_role = self._roles.setdefault(role, IdpRole(name=role))
                created.append(idp_role)
            return created

    async def revoke_role(self, user_id: str, role: str) -> bool:
        async with self._lock:
            user = self._users.get(user_id)
            if user is None or role not in user.roles:
                return False
            user.roles.remove(role)
        return True

    async def list_roles(self) -> list[IdpRole]:
        async with self._lock:
            return list(self._roles.values())

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _verify_totp(secret: str, code: str) -> None:
        """Verify a TOTP *code* against *secret*; raise :class:`PermissionError` on failure."""
        try:
            import pyotp  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            msg = "TOTP MFA requires pyotp — `pip install pyfly[security]`"
            raise ImportError(msg) from exc
        if not pyotp.TOTP(secret).verify(code):
            msg = "invalid MFA code"
            raise PermissionError(msg)

    @staticmethod
    def _hash(password: str) -> bytes:
        try:
            import bcrypt  # type: ignore[import-not-found, unused-ignore]

            return bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        except Exception:  # noqa: BLE001
            # Fallback: salted SHA-256 (NOT SUITABLE FOR PRODUCTION).
            import hashlib

            salt = uuid.uuid4().hex.encode()
            return salt + hashlib.sha256(salt + password.encode()).digest()

    @staticmethod
    def _verify(password: str, hashed: bytes) -> bool:
        try:
            import bcrypt  # type: ignore[import-not-found, unused-ignore]

            return bool(bcrypt.checkpw(password.encode(), hashed))
        except Exception:  # noqa: BLE001
            import hashlib

            salt, digest = hashed[:32], hashed[32:]
            return hashlib.sha256(salt + password.encode()).digest() == digest
