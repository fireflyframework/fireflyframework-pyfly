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
"""Tests for PasswordEncoder protocol and encoder adapters."""

from __future__ import annotations

from pyfly.security.password import (
    BcryptPasswordEncoder,
    DelegatingPasswordEncoder,
    PasswordEncoder,
    Pbkdf2PasswordEncoder,
    ScryptPasswordEncoder,
    create_delegating_password_encoder,
)


class TestBcryptPasswordEncoder:
    def test_hash_produces_bcrypt_output(self):
        encoder = BcryptPasswordEncoder(rounds=4)
        hashed = encoder.hash("my-secret-password")
        assert hashed.startswith("$2b$")

    def test_verify_correct_password(self):
        encoder = BcryptPasswordEncoder(rounds=4)
        hashed = encoder.hash("correct-password")
        assert encoder.verify("correct-password", hashed) is True

    def test_verify_wrong_password(self):
        encoder = BcryptPasswordEncoder(rounds=4)
        hashed = encoder.hash("correct-password")
        assert encoder.verify("wrong-password", hashed) is False

    def test_different_passwords_different_hashes(self):
        encoder = BcryptPasswordEncoder(rounds=4)
        hash1 = encoder.hash("same-password")
        hash2 = encoder.hash("same-password")
        assert hash1 != hash2

    def test_custom_rounds(self):
        encoder = BcryptPasswordEncoder(rounds=4)
        hashed = encoder.hash("test")
        assert "$04$" in hashed
        assert encoder.verify("test", hashed) is True

    def test_protocol_conformance(self):
        encoder = BcryptPasswordEncoder()
        assert isinstance(encoder, PasswordEncoder)

    def test_empty_password_hashes(self):
        encoder = BcryptPasswordEncoder(rounds=4)
        hashed = encoder.hash("")
        assert hashed.startswith("$2b$")
        assert encoder.verify("", hashed) is True
        assert encoder.verify("non-empty", hashed) is False


class TestPbkdf2PasswordEncoder:
    def test_round_trip(self):
        enc = Pbkdf2PasswordEncoder(iterations=10_000)
        hashed = enc.hash("pw")
        assert enc.verify("pw", hashed) is True
        assert enc.verify("nope", hashed) is False

    def test_self_describing_format(self):
        enc = Pbkdf2PasswordEncoder(iterations=10_000)
        assert enc.hash("pw").startswith("sha256$10000$")

    def test_salt_is_random(self):
        enc = Pbkdf2PasswordEncoder(iterations=10_000)
        assert enc.hash("pw") != enc.hash("pw")

    def test_protocol_conformance(self):
        assert isinstance(Pbkdf2PasswordEncoder(), PasswordEncoder)

    def test_corrupt_hash_returns_false(self):
        assert Pbkdf2PasswordEncoder().verify("pw", "not-a-valid-hash") is False


class TestScryptPasswordEncoder:
    def test_round_trip(self):
        enc = ScryptPasswordEncoder(n=2**14)
        hashed = enc.hash("pw")
        assert enc.verify("pw", hashed) is True
        assert enc.verify("bad", hashed) is False

    def test_protocol_conformance(self):
        assert isinstance(ScryptPasswordEncoder(), PasswordEncoder)

    def test_corrupt_hash_returns_false(self):
        assert ScryptPasswordEncoder().verify("pw", "garbage") is False


class TestDelegatingPasswordEncoder:
    def _enc(self) -> DelegatingPasswordEncoder:
        return DelegatingPasswordEncoder(
            {"bcrypt": BcryptPasswordEncoder(rounds=4), "pbkdf2": Pbkdf2PasswordEncoder(iterations=10_000)},
            encoding_id="bcrypt",
        )

    def test_hash_is_prefixed_with_default_id(self):
        assert self._enc().hash("pw").startswith("{bcrypt}$2b$")

    def test_verify_round_trip(self):
        enc = self._enc()
        assert enc.verify("pw", enc.hash("pw")) is True
        assert enc.verify("bad", enc.hash("pw")) is False

    def test_verify_dispatches_by_prefix(self):
        enc = self._enc()
        inner = Pbkdf2PasswordEncoder(iterations=10_000).hash("pw")
        assert enc.verify("pw", "{pbkdf2}" + inner) is True
        assert enc.verify("bad", "{pbkdf2}" + inner) is False

    def test_unknown_prefix_returns_false(self):
        assert self._enc().verify("pw", "{md5}deadbeef") is False

    def test_missing_prefix_returns_false(self):
        assert self._enc().verify("pw", "$2b$unprefixed") is False

    def test_upgrade_encoding_true_for_non_default_id(self):
        enc = self._enc()
        stored = "{pbkdf2}" + Pbkdf2PasswordEncoder(iterations=10_000).hash("pw")
        assert enc.upgrade_encoding(stored) is True

    def test_upgrade_encoding_false_for_default_id(self):
        enc = self._enc()
        assert enc.upgrade_encoding(enc.hash("pw")) is False

    def test_upgrade_encoding_true_for_unprefixed(self):
        assert self._enc().upgrade_encoding("$2b$legacy") is True

    def test_unknown_default_encoding_id_rejected(self):
        import pytest

        with pytest.raises(ValueError, match="encoding_id"):
            DelegatingPasswordEncoder({"bcrypt": BcryptPasswordEncoder(rounds=4)}, encoding_id="pbkdf2")

    def test_protocol_conformance(self):
        assert isinstance(self._enc(), PasswordEncoder)


class TestPasswordEncoderFactory:
    def test_create_delegating_default_is_bcrypt(self):
        enc = create_delegating_password_encoder(bcrypt_rounds=4)
        hashed = enc.hash("pw")
        assert hashed.startswith("{bcrypt}")
        assert enc.verify("pw", hashed) is True

    def test_create_delegating_recognizes_pbkdf2_and_scrypt(self):
        enc = create_delegating_password_encoder(bcrypt_rounds=4)
        pbkdf2 = "{pbkdf2}" + Pbkdf2PasswordEncoder(iterations=10_000).hash("pw")
        scrypt = "{scrypt}" + ScryptPasswordEncoder(n=2**14).hash("pw")
        assert enc.verify("pw", pbkdf2) is True
        assert enc.verify("pw", scrypt) is True


class TestDelegatingEncoderAutoConfig:
    def test_opt_in_provides_delegating_encoder(self):
        from pyfly.core.config import Config
        from pyfly.security.auto_configuration import PasswordEncoderAutoConfiguration

        cfg = Config(
            {"pyfly": {"security": {"password": {"delegating": {"enabled": "true"}, "bcrypt-rounds": 4}}}}
        )
        enc = PasswordEncoderAutoConfiguration().delegating_password_encoder(cfg)
        hashed = enc.hash("pw")
        assert hashed.startswith("{bcrypt}")
        assert enc.verify("pw", hashed) is True
