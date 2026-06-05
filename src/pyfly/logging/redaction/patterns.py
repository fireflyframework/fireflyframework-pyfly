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
"""Built-in PII detection patterns + validators."""

from __future__ import annotations

import re
from collections.abc import Callable

_DIGITS = re.compile(r"\D")


def luhn_valid(value: str) -> bool:
    """True when *value*'s digits pass the Luhn checksum (credit cards)."""
    digits = [int(c) for c in _DIGITS.sub("", value)]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# Compiled built-in PII patterns, keyed by entity name.
BUILTIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "CREDIT_CARD": re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    "BEARER_TOKEN": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    "URL_CREDENTIALS": re.compile(r"://[^/\s:@]+:([^/\s:@]+)@"),
    "PHONE": re.compile(r"(?<!\d)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3}[ .-]?\d{4}(?!\d)"),
    "IPV4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"),
    "IPV6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
}

# Optional per-entity validators (a match is only redacted when the validator passes).
VALIDATORS: dict[str, Callable[[str], bool]] = {
    "CREDIT_CARD": luhn_valid,
}
