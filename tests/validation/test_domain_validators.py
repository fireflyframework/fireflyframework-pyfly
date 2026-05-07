# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the domain validators (IBAN, BIC, phone, etc.)."""

from __future__ import annotations

import pytest

from pyfly.validation.domain import (
    is_strong_password,
    is_valid_amount,
    is_valid_bic,
    is_valid_credit_card,
    is_valid_currency_code,
    is_valid_iban,
    is_valid_phone_number,
    is_valid_pin,
    valid_iban,
)


class TestIban:
    def test_valid_iban(self) -> None:
        # GB82 WEST 1234 5698 7654 32 — well-known test IBAN
        assert is_valid_iban("GB82WEST12345698765432")

    def test_invalid_checksum(self) -> None:
        assert not is_valid_iban("GB82WEST12345698765431")

    def test_callable_returns_value(self) -> None:
        assert valid_iban("GB82WEST12345698765432") == "GB82WEST12345698765432"

    def test_callable_raises(self) -> None:
        with pytest.raises(ValueError):
            valid_iban("nope")


class TestBic:
    def test_valid_8_char(self) -> None:
        assert is_valid_bic("DEUTDEFF")

    def test_valid_11_char(self) -> None:
        assert is_valid_bic("DEUTDEFF500")

    def test_invalid(self) -> None:
        assert not is_valid_bic("X")


class TestPhone:
    def test_valid_e164(self) -> None:
        assert is_valid_phone_number("+15555550100")
        assert is_valid_phone_number("+447911123456")

    def test_invalid(self) -> None:
        assert not is_valid_phone_number("not-a-phone")


class TestCreditCard:
    def test_visa_test_card(self) -> None:
        assert is_valid_credit_card("4242 4242 4242 4242")

    def test_invalid_luhn(self) -> None:
        assert not is_valid_credit_card("4242 4242 4242 4243")


class TestCurrency:
    def test_iso(self) -> None:
        assert is_valid_currency_code("USD")
        assert not is_valid_currency_code("usd")
        assert not is_valid_currency_code("US")


class TestAmount:
    def test_positive(self) -> None:
        assert is_valid_amount(123.45)

    def test_zero_with_flag(self) -> None:
        assert not is_valid_amount(0)
        assert is_valid_amount(0, allow_zero=True)


class TestPasswordStrength:
    def test_strong(self) -> None:
        assert is_strong_password("Abcdef1!")

    def test_weak(self) -> None:
        assert not is_strong_password("abc")


class TestPin:
    def test_default(self) -> None:
        assert is_valid_pin("1234")
        assert not is_valid_pin("12345")
