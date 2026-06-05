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

from pyfly.config.properties.logging import RedactionProperties
from pyfly.logging.redaction.engine import RegexRedactor, build_redactor


def test_placeholder_mask():
    r = RegexRedactor(["EMAIL"], mask="placeholder")
    assert r.redact("ping jane@acme.io ok") == "ping <EMAIL> ok"


def test_partial_mask():
    r = RegexRedactor(["CREDIT_CARD"], mask="partial")
    out = r.redact("card 4111 1111 1111 1111 end")
    assert out.endswith("1111 end") and "*" in out


def test_extra_patterns():
    r = RegexRedactor([], mask="placeholder", extra_patterns={"EMP": r"EMP-\d{4}"})
    assert r.redact("id EMP-1234 x") == "id <EMP> x"


def test_non_string_passthrough():
    r = RegexRedactor(["EMAIL"])
    assert r.redact(123) == 123  # type: ignore[arg-type]


def test_build_redactor_disabled_returns_none():
    props = RedactionProperties(enabled=False)
    assert build_redactor(props) is None


def test_build_redactor_regex_engine():
    props = RedactionProperties(engine="regex")
    r = build_redactor(props)
    assert r is not None
    assert type(r).__name__ == "RegexRedactor"


def test_build_redactor_auto_falls_back_to_regex_without_presidio():
    props = RedactionProperties(engine="auto")
    r = build_redactor(props)
    assert r is not None
    # presidio is not installed in CI -> regex fallback
    assert type(r).__name__ in ("RegexRedactor", "PresidioRedactor")
