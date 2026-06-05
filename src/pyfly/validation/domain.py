# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Domain-specific validators — IBAN, BIC, phone, credit card, currency, etc.

Mirrors ``org.fireflyframework.validators``.  Each function returns ``True``
when the input is valid.  Callable shortcuts let you build pydantic
``field_validator`` rules with one line.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# IBAN: country (2 letters) + 2 check digits + alphanumeric BBAN, total length
# is country-specific (15..34) but here we just enforce general format and the
# canonical mod-97 checksum.
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{1,30}$")
_BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_PHONE_E164 = re.compile(r"^\+?[1-9]\d{6,14}$")
_TAX_ID_GENERIC = re.compile(r"^[A-Z0-9]{3,20}$")

# IBAN total length per ISO 13616 country code (audit #193).
_IBAN_LENGTHS = {
    "AD": 24,
    "AE": 23,
    "AL": 28,
    "AT": 20,
    "AZ": 28,
    "BA": 20,
    "BE": 16,
    "BG": 22,
    "BH": 22,
    "BR": 29,
    "BY": 28,
    "CH": 21,
    "CR": 22,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "DO": 28,
    "EE": 20,
    "EG": 29,
    "ES": 24,
    "FI": 18,
    "FO": 18,
    "FR": 27,
    "GB": 22,
    "GE": 22,
    "GI": 23,
    "GL": 18,
    "GR": 27,
    "GT": 28,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IL": 23,
    "IQ": 23,
    "IS": 26,
    "IT": 27,
    "JO": 30,
    "KW": 30,
    "KZ": 20,
    "LB": 28,
    "LC": 32,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "MR": 27,
    "MT": 31,
    "MU": 30,
    "NL": 18,
    "NO": 15,
    "PK": 24,
    "PL": 28,
    "PS": 29,
    "PT": 25,
    "QA": 29,
    "RO": 24,
    "RS": 22,
    "SA": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "TN": 24,
    "TR": 26,
    "UA": 29,
    "VG": 24,
    "XK": 20,
}

# Active ISO 4217 currency codes (audit #191).
_ISO_4217_CODES = (
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL "
    "BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP "
    "ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS "
    "INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD "
    "LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK "
    "NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK "
    "SGD SHP SLE SOS SRD SSP STN SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX "
    "USD UYU UZS VED VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL"
)
_ISO_4217 = frozenset(_ISO_4217_CODES.split())

# Card-scheme prefix patterns (audit #192).
_CARD_SCHEMES = (
    re.compile(r"^4\d{12}(\d{3})?(\d)?$"),  # Visa
    re.compile(r"^(5[1-5]\d{14}|2(2[2-9]\d{12}|[3-6]\d{13}|7[01]\d{12}|720\d{12}))$"),  # Mastercard
    re.compile(r"^3[47]\d{13}$"),  # Amex
    re.compile(r"^(6011\d{12}|65\d{14}|64[4-9]\d{13})$"),  # Discover
    re.compile(r"^3(0[0-5]|[68]\d)\d{11}$"),  # Diners Club
    re.compile(r"^(352[89]|35[3-8]\d)\d{12}$"),  # JCB
)


def is_valid_iban(value: str | None) -> bool:
    if value is None:
        return False
    s = value.replace(" ", "").upper()
    if not _IBAN_RE.match(s):
        return False
    # Enforce the country-specific total length (audit #193).
    expected = _IBAN_LENGTHS.get(s[:2])
    if expected is None or len(s) != expected:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def is_valid_bic(value: str | None) -> bool:
    return bool(value and _BIC_RE.match(value.upper()))


def is_valid_phone_number(value: str | None) -> bool:
    if value is None:
        return False
    return bool(_PHONE_E164.match(value.replace(" ", "").replace("-", "")))


def is_valid_credit_card(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.replace(" ", "").replace("-", "")
    digits = [int(d) for d in normalized if d.isdigit()]
    if len(digits) < 12 or len(digits) > 19:
        return False
    # Require a recognized card scheme before accepting (audit #192) — a
    # Luhn-valid number of no known type is not a valid card.
    if not any(scheme.match(normalized) for scheme in _CARD_SCHEMES):
        return False
    # Luhn checksum.
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_cvv(value: str | None) -> bool:
    return bool(value and value.isdigit() and 3 <= len(value) <= 4)


def is_valid_currency_code(value: str | None) -> bool:
    """ISO 4217 — must be an active three-letter currency code (audit #191)."""
    return bool(value and value in _ISO_4217)


def is_valid_amount(value: object, *, allow_zero: bool = False, max_digits: int = 18) -> bool:
    """Numeric amount with bounded precision."""
    try:
        decimal_value = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if decimal_value < 0:
        return False
    if decimal_value == 0 and not allow_zero:
        return False
    return len(str(int(decimal_value))) <= max_digits


def is_valid_account_number(value: str | None) -> bool:
    return bool(value and value.isalnum() and 6 <= len(value) <= 34)


def is_valid_tax_id(value: str | None) -> bool:
    return bool(value and _TAX_ID_GENERIC.match(value.upper()))


def is_valid_pin(value: str | None, *, length: int = 4) -> bool:
    return bool(value and value.isdigit() and len(value) == length)


def is_strong_password(value: str | None, *, min_length: int = 8) -> bool:
    if value is None or len(value) < min_length:
        return False
    has_lower = any(c.islower() for c in value)
    has_upper = any(c.isupper() for c in value)
    has_digit = any(c.isdigit() for c in value)
    has_symbol = any(not c.isalnum() for c in value)
    return has_lower and has_upper and has_digit and has_symbol


def is_valid_date(value: str | None, *, fmt: str = "%Y-%m-%d") -> bool:
    """ISO-8601 calendar date by default."""
    if value is None:
        return False
    from datetime import datetime

    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def is_valid_datetime(value: str | None) -> bool:
    """ISO-8601 datetime — accepts ``2026-05-07T12:00:00``, ``…+00:00`` or trailing ``Z``."""
    if value is None:
        return False
    from datetime import datetime

    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def is_valid_national_id(value: str | None) -> bool:
    """Generic national-id format: 5–20 alphanumerics (countries plug in their own checks)."""
    if value is None:
        return False
    s = value.replace(" ", "").replace("-", "").upper()
    return bool(s) and 5 <= len(s) <= 20 and s.isalnum()


def is_valid_sort_code(value: str | None) -> bool:
    """UK bank sort code — 6 digits, optionally separated by ``-``."""
    if value is None:
        return False
    digits = value.replace("-", "").replace(" ", "")
    return digits.isdigit() and len(digits) == 6


def is_valid_interest_rate(value: object, *, min_pct: float = 0.0, max_pct: float = 100.0) -> bool:
    """Percentage (e.g., 4.25 = 4.25%) within an allowed band."""
    try:
        pct = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return min_pct <= pct <= max_pct


# --- Pydantic-friendly validator factories ------------------------------------


def _factory(predicate: Callable[..., bool], message: str) -> Callable[[object], object]:
    def _validate(value: object) -> object:
        if predicate(value):
            return value
        raise ValueError(message)

    return _validate


valid_iban = _factory(is_valid_iban, "invalid IBAN")
valid_bic = _factory(is_valid_bic, "invalid BIC")
valid_phone_number = _factory(is_valid_phone_number, "invalid phone number")
valid_credit_card = _factory(is_valid_credit_card, "invalid credit card number")
valid_cvv = _factory(is_valid_cvv, "invalid CVV")
valid_currency_code = _factory(is_valid_currency_code, "invalid currency code")
valid_amount = _factory(is_valid_amount, "invalid amount")
valid_account_number = _factory(is_valid_account_number, "invalid account number")
valid_tax_id = _factory(is_valid_tax_id, "invalid tax id")
valid_pin = _factory(is_valid_pin, "invalid pin")
valid_strong_password = _factory(is_strong_password, "password is not strong enough")
valid_date = _factory(is_valid_date, "invalid date")
valid_datetime = _factory(is_valid_datetime, "invalid datetime")
valid_national_id = _factory(is_valid_national_id, "invalid national id")
valid_sort_code = _factory(is_valid_sort_code, "invalid sort code")
valid_interest_rate = _factory(is_valid_interest_rate, "interest rate out of range")
