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
    # A phone number, and only a phone number. The guards are ``(?<![\\w+])`` /
    # ``(?!\\w)`` rather than the digit-only ``(?<!\\d)`` / ``(?!\\d)`` this rule used
    # until 26.09.07: a hex letter next to a digit run satisfies a digit-only guard,
    # which is why the middle of a uuid used to read as a phone number. The compact
    # E.164 branch is new too — eleven contiguous digits cannot be split by
    # ``\\d{3}\\d{4}``, so ``+34911234567`` used to walk straight through.
    "PHONE": re.compile(
        r"(?<![\w+])(?:"
        r"\+\d{7,15}"  # compact E.164: +34911234567
        r"|\+\d{1,3}(?:[ .-]\d{1,4}){2,5}"  # separated international: +1-202-555-0143
        r"|\(\d{2,4}\)[ .-]?\d{3}[ .-]?\d{4}"  # (212) 555-0143
        r"|\d{3}[ .-]\d{3}[ .-]\d{4}"  # 202-555-0143
        r"|\d{3}[ .-]\d{4}"  # 555-0143
        r")(?!\w)"
    ),
    "IPV4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"),
    "IPV6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
}

# Optional per-entity validators (a match is only redacted when the validator passes).
VALIDATORS: dict[str, Callable[[str], bool]] = {
    "CREDIT_CARD": luhn_valid,
}


# Identifier shapes that are NEVER PII and are therefore protected from every
# entity pattern above. A uuid, a W3C trace id and a ULID are digits and hex
# letters by construction, so a generic PII rule can match a substring of one
# and mangle it — silently, and only sometimes, which is worse than mangling it
# always: a run id grepped out of an application never matches the log line, and
# the operator concludes the event never happened. ``processor._NEVER_REDACT``
# already applies this principle to the structlog correlation FIELDS; these
# patterns apply it to the identifiers carried in the message TEXT, wherever
# they appear.
#
# The 16- and 32-char hex shapes require at least one hex LETTER on purpose: an
# all-digit run of that length is a credit-card number far more often than it is
# a span id, and protecting it would turn this guard into a way to hide PII.
_HEX_ID = r"\b(?=[0-9a-fA-F]{{{n}}}\b)[0-9a-fA-F]*[a-fA-F][0-9a-fA-F]*\b"

PRESERVE_PATTERNS: dict[str, re.Pattern[str]] = {
    # uuid, any version, either case
    "UUID": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # W3C trace-context trace id (32 hex) and span id (16 hex)
    "TRACE_ID": re.compile(_HEX_ID.format(n=32)),
    "SPAN_ID": re.compile(_HEX_ID.format(n=16)),
    # ULID — Crockford base32, 26 chars, first char 0-7
    "ULID": re.compile(r"\b[0-7][0-9A-HJKMNP-TV-Z]{25}\b"),
}
