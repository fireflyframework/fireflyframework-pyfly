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
from __future__ import annotations

from pyfly.logging.redaction.patterns import BUILTIN_PATTERNS, VALIDATORS, luhn_valid


def test_patterns_present():
    for ent in ("EMAIL", "CREDIT_CARD", "IBAN", "US_SSN", "JWT", "BEARER_TOKEN", "URL_CREDENTIALS", "PHONE", "IPV4"):
        assert ent in BUILTIN_PATTERNS


def test_email_matches():
    assert BUILTIN_PATTERNS["EMAIL"].search("contact jane.doe@acme.io now")


def test_luhn():
    assert luhn_valid("4111111111111111") is True  # valid test Visa
    assert luhn_valid("4111111111111112") is False
    assert VALIDATORS["CREDIT_CARD"]("4111 1111 1111 1111") is True


def test_credit_card_validator_rejects_random_16_digits():
    assert VALIDATORS["CREDIT_CARD"]("1234567890123456") is False
