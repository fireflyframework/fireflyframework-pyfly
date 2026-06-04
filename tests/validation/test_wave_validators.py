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
"""Regression tests for stricter domain validators (#191, #192, #193)."""

from __future__ import annotations

from pyfly.validation.domain import is_valid_credit_card, is_valid_currency_code, is_valid_iban


def test_currency_code_rejects_unknown() -> None:
    assert is_valid_currency_code("USD") is True
    assert is_valid_currency_code("XYZ") is False  # audit #191 — not ISO 4217
    assert is_valid_currency_code("US") is False


def test_iban_rejects_wrong_country_length() -> None:
    # A valid German IBAN is 22 chars; a too-short one must fail (audit #193).
    assert is_valid_iban("DE89370400440532013000") is True
    assert is_valid_iban("DE8937040044053201") is False  # wrong length for DE
    assert is_valid_iban("ZZ8937040044053201300") is False  # unknown country


def test_credit_card_requires_known_scheme() -> None:
    assert is_valid_credit_card("4111111111111111") is True  # Visa
    # A Luhn-valid number with no recognized scheme prefix is rejected (#192).
    assert is_valid_credit_card("9999999999999995") is False
