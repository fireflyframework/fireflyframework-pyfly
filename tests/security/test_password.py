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
"""Tests for PasswordEncoder protocol and BcryptPasswordEncoder adapter."""

from __future__ import annotations

from pyfly.security.password import BcryptPasswordEncoder, PasswordEncoder


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
