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
"""AuthenticationManager / AuthenticationProvider SPI."""

from __future__ import annotations

import pytest

from pyfly.security.authentication import (
    Authentication,
    AuthenticationException,
    BadCredentialsException,
    DaoAuthenticationProvider,
    DisabledException,
    ProviderManager,
)
from pyfly.security.password import BcryptPasswordEncoder
from pyfly.security.user_details import InMemoryUserDetailsService, UserDetails

_ENCODER = BcryptPasswordEncoder(rounds=4)


def _provider() -> DaoAuthenticationProvider:
    service = InMemoryUserDetailsService(
        UserDetails(username="alice", password_hash=_ENCODER.hash("pw"), roles=["ADMIN"], permissions=["read"]),
        UserDetails(username="bob", password_hash=_ENCODER.hash("pw"), enabled=False),
    )
    return DaoAuthenticationProvider(service, _ENCODER)


class TestDaoAuthenticationProvider:
    @pytest.mark.asyncio
    async def test_valid_credentials_authenticates(self) -> None:
        result = await _provider().authenticate(Authentication(principal="alice", credentials="pw"))
        assert result.authenticated is True
        assert result.principal == "alice"
        assert "ADMIN" in result.authorities
        assert "read" in result.authorities
        assert result.credentials is None  # erased after authentication

    @pytest.mark.asyncio
    async def test_wrong_password_raises_bad_credentials(self) -> None:
        with pytest.raises(BadCredentialsException):
            await _provider().authenticate(Authentication(principal="alice", credentials="WRONG"))

    @pytest.mark.asyncio
    async def test_unknown_user_raises_bad_credentials(self) -> None:
        with pytest.raises(BadCredentialsException):
            await _provider().authenticate(Authentication(principal="ghost", credentials="pw"))

    @pytest.mark.asyncio
    async def test_disabled_user_raises_disabled(self) -> None:
        with pytest.raises(DisabledException):
            await _provider().authenticate(Authentication(principal="bob", credentials="pw"))

    def test_supports_password_authentication(self) -> None:
        assert _provider().supports(Authentication(principal="x", credentials="y")) is True
        assert _provider().supports(Authentication(principal="x", credentials=None)) is False


class TestProviderManager:
    @pytest.mark.asyncio
    async def test_delegates_to_supporting_provider(self) -> None:
        manager = ProviderManager(_provider())
        result = await manager.authenticate(Authentication(principal="alice", credentials="pw"))
        assert result.authenticated is True
        assert result.credentials is None

    @pytest.mark.asyncio
    async def test_no_supporting_provider_raises(self) -> None:
        manager = ProviderManager(_provider())
        with pytest.raises(AuthenticationException):
            await manager.authenticate(Authentication(principal="x", credentials=None))

    @pytest.mark.asyncio
    async def test_to_security_context(self) -> None:
        manager = ProviderManager(_provider())
        result = await manager.authenticate(Authentication(principal="alice", credentials="pw"))
        ctx = result.to_security_context()
        assert ctx.user_id == "alice"
        assert ctx.is_authenticated
