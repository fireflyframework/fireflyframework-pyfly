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
"""An identifier in a log message survives redaction; PII in the same line does not.

The defect these tests pin: the ``PHONE`` rule was guarded by digits only, and a
hex letter or a hyphen satisfies a digit-only guard — so roughly one uuid in four
had a digit run in its middle replaced by ``<PHONE>`` and a run id grepped out of
an application never matched its own log line. The same rule could not match
compact E.164, so it gave up searchability AND privacy at once.
"""

from __future__ import annotations

import logging
import random
import re
import secrets
import uuid

import pytest

from pyfly.config.properties.logging import RedactionProperties
from pyfly.logging.redaction.engine import RegexRedactor, build_redactor, protected_spans
from pyfly.logging.redaction.patterns import BUILTIN_PATTERNS, PRESERVE_PATTERNS
from pyfly.logging.redaction.processor import RedactionFilter

# A redactor with exactly the framework defaults — what a service gets with no
# configuration at all, which is the configuration the defect was reported under.
DEFAULT_PROPS = RedactionProperties(engine="regex")


def default_redactor() -> RegexRedactor:
    redactor = build_redactor(DEFAULT_PROPS)
    assert isinstance(redactor, RegexRedactor)
    return redactor


def uuid7() -> str:
    """A uuid7-shaped id (48-bit time prefix, version 7, variant 10)."""
    raw = bytearray(random.getrandbits(48).to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


# (label, token, redacted?) — one row per shape that turns up in a real log line.
SHAPES = [
    ("uuid4", "550e8400-e29b-41d4-a716-446655440000", False),
    ("uuid4 upper", "550E8400-E29B-41D4-A716-446655440000", False),
    ("uuid7", "01a0b263-b0b7-71f5-bfb2-3b2aa0f1c2d3", False),
    ("uuid1", "c232ab00-9414-11ec-b3c8-9f6bdeced846", False),
    ("w3c trace id", "4bf92f3577b34da6a3ce929d0e0e4736", False),
    ("w3c span id", "00f067aa0ba902b7", False),
    ("ulid", "01ARZ3NDEKTSV4RRFFQ69G5FAV", False),
    ("iso timestamp", "2026-09-24T07:22:54.123456+00:00", False),
    ("sha256", "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881", False),
    ("base64url token", "HBExZ1mD6Mc6iSGncklO0SvGeXsqj3YTDwjgJRMTmwo", False),
    ("ipv4 address", "192.168.1.100", False),
    ("e.164 phone (es)", "+34911234567", True),
    ("e.164 phone (uk)", "+442071838750", True),
    ("e.164 phone (cn)", "+8613800138000", True),
    ("international phone", "+1-202-555-0143", True),
    ("national phone, spaces", "202 555 0143", True),
    ("national phone, dashes", "202-555-0143", True),
    ("bracketed area code", "(212) 555-0143", True),
    ("16-digit card", "4111111111111111", True),
    ("16-digit card, spaced", "4111 1111 1111 1111", True),
]


@pytest.mark.parametrize(("label", "token", "redacted"), SHAPES, ids=[row[0] for row in SHAPES])
def test_shape_is_redacted_only_when_it_is_pii(label: str, token: str, redacted: bool) -> None:
    line = f"processing job token={token} ok"
    out = default_redactor().redact(line)
    if redacted:
        assert token not in out, f"{label} leaked through redaction: {out}"
    else:
        assert out == line, f"{label} was mangled by redaction: {out}"


def test_the_reported_line_is_searchable_again() -> None:
    """The exact shape the defect report carried out of a running service."""
    line = "run 01a0b263-b0b7-71f5-bfb2-3b2aa0f1c2d3 started"
    assert default_redactor().redact(line) == line
    assert "<PHONE>" not in default_redactor().redact(line)


@pytest.mark.parametrize("make", [lambda: str(uuid.uuid4()), uuid7], ids=["uuid4", "uuid7"])
def test_no_generated_uuid_is_ever_mangled(make) -> None:  # type: ignore[no-untyped-def]
    """A sweep, not a handful of examples.

    The old rule mangled about a quarter of all uuids, so a few hand-picked ids
    would pass by luck. Only a sweep pins the behaviour.
    """
    redactor = default_redactor()
    for _ in range(3000):
        line = f"run {make()} finished"
        assert redactor.redact(line) == line


def test_trace_and_span_ids_survive_in_the_message_text() -> None:
    """``_NEVER_REDACT`` covers structlog FIELD keys; this is the message half."""
    line = "trace_id=4bf92f3577b34da6a3ce929d0e0e4736 span_id=00f067aa0ba902b7 handled"
    assert default_redactor().redact(line) == line


def test_a_uuid_and_a_phone_number_on_one_line() -> None:
    line = "run 550e8400-e29b-41d4-a716-446655440000 caller +34911234567"
    out = default_redactor().redact(line)
    assert "550e8400-e29b-41d4-a716-446655440000" in out
    assert "+34911234567" not in out
    assert "<PHONE>" in out


def test_a_real_phone_number_still_leaves_the_log_line() -> None:
    """The filter attached to every handler, not just the engine underneath it."""
    redactor = default_redactor()
    log_filter = RedactionFilter(redactor, DEFAULT_PROPS.allow_fields, DEFAULT_PROPS.deny_fields)
    record = logging.LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="run %s called %s",
        args=("550e8400-e29b-41d4-a716-446655440000", "+34911234567"),
        exc_info=None,
    )
    assert log_filter.filter(record) is True
    rendered = record.getMessage()
    assert "+34911234567" not in rendered
    assert "<PHONE>" in rendered
    assert "550e8400-e29b-41d4-a716-446655440000" in rendered


def test_a_card_number_is_not_hidden_by_the_span_id_shape() -> None:
    """A bare 16-digit card is 16 hex characters too — the letter requirement is load-bearing."""
    assert protected_spans("card 4111111111111111 end", list(PRESERVE_PATTERNS.values())) == []
    assert "4111111111111111" not in default_redactor().redact("card 4111111111111111 end")


def test_a_random_16_digit_run_is_still_not_a_card() -> None:
    """``VALIDATORS['CREDIT_CARD']`` must keep deciding — the Luhn gate has not moved."""
    line = "order 1234567890123456 placed"
    assert default_redactor().redact(line) == line


class TestPhonePattern:
    """The rule itself, at the level the report argued about."""

    def test_compact_e164_matches(self) -> None:
        for number in ("+34911234567", "+442071838750", "+8613800138000"):
            assert BUILTIN_PATTERNS["PHONE"].search(number), number

    def test_the_guards_are_word_boundaries_not_digit_boundaries(self) -> None:
        # A hyphen and a hex letter both satisfy the old (?<!\d)/(?!\d) guards.
        assert not BUILTIN_PATTERNS["PHONE"].search("-9277-4401-")
        assert not BUILTIN_PATTERNS["PHONE"].search("a2025550143b")

    def test_seven_bare_digits_are_no_longer_a_phone_number(self) -> None:
        # Deliberate: an unseparated 7-digit run is an id or a count far more
        # often than a phone number, and the separated forms still match.
        assert not BUILTIN_PATTERNS["PHONE"].search("rows 5550143 done")
        assert BUILTIN_PATTERNS["PHONE"].search("call 555-0143 now")


class TestPreservePatterns:
    """``pyfly.logging.redaction.preserve-patterns`` — the consumer's own id shapes."""

    def test_a_consumer_pattern_is_protected(self) -> None:
        # Without the allow-list this reads as a phone number and is destroyed.
        naive = RegexRedactor(["PHONE"], preserve_patterns={})
        assert naive.redact("ref ORD-555-0143 shipped") != "ref ORD-555-0143 shipped"

        redactor = RegexRedactor(["PHONE"], preserve_patterns={"ORDER_REF": r"ORD-\d{3}-\d{4}"})
        assert redactor.redact("ref ORD-555-0143 shipped") == "ref ORD-555-0143 shipped"

    def test_an_invalid_preserve_pattern_is_warned_and_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="pyfly.logging"):
            redactor = RegexRedactor(["PHONE"], preserve_patterns={"BROKEN": "([unclosed"})
        assert "BROKEN" in caplog.text
        # The redactor is still usable, and the built-in shapes still apply.
        assert redactor.redact("call +34911234567") == "call <PHONE>"
        assert redactor.redact("run 550e8400-e29b-41d4-a716-446655440000") == (
            "run 550e8400-e29b-41d4-a716-446655440000"
        )

    def test_the_property_reaches_the_built_redactor(self) -> None:
        props = RedactionProperties(engine="regex", preserve_patterns={"ORDER_REF": r"ORD-\d{3}-\d{4}"})
        redactor = build_redactor(props)
        assert redactor is not None
        assert redactor.redact("ref ORD-555-0143") == "ref ORD-555-0143"


class TestProtectedSpanSplicing:
    """Redaction must never crash logging, whatever the spans look like."""

    PATTERNS = [re.compile(r"ID-\d+")]

    def test_span_at_index_zero(self) -> None:
        assert protected_spans("ID-1 tail", self.PATTERNS) == [(0, 4)]
        assert RegexRedactor(["PHONE"], preserve_patterns={"X": r"ID-\d+"}).redact("ID-1 555-0143") == "ID-1 <PHONE>"

    def test_span_at_end_of_string(self) -> None:
        redactor = RegexRedactor(["PHONE"], preserve_patterns={"X": r"ID-\d+"})
        assert redactor.redact("555-0143 ID-1") == "<PHONE> ID-1"

    def test_two_adjacent_spans_are_merged(self) -> None:
        spans = protected_spans("ID-1ID-2", self.PATTERNS)
        assert spans == [(0, 8)]

    def test_overlapping_spans_from_different_patterns_are_merged(self) -> None:
        patterns = [re.compile(r"abc\d"), re.compile(r"c\d\d")]
        assert protected_spans("xabc12y", patterns) == [(1, 6)]

    def test_whole_string_protected(self) -> None:
        redactor = RegexRedactor(["PHONE"], preserve_patterns={"X": r"^555-0143$"})
        assert redactor.redact("555-0143") == "555-0143"

    def test_empty_and_non_string_input(self) -> None:
        redactor = default_redactor()
        assert redactor.redact("") == ""
        assert redactor.redact(None) is None  # type: ignore[arg-type]
