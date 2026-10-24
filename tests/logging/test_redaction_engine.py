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

import builtins

import pytest

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


@pytest.mark.parametrize("engine", ["regex", "auto"])
def test_build_redactor_uses_native_patterns_without_loading_nlp(engine, monkeypatch):
    original_import = builtins.__import__

    def import_without_nlp(name, *args, **kwargs):
        if name.startswith(("presidio", "spacy")):
            pytest.fail(f"Native redaction attempted to import {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_nlp)
    r = build_redactor(RedactionProperties(engine=engine))
    assert r is not None
    assert isinstance(r, RegexRedactor)
    assert r.redact("Contact jane@acme.io for run 550e8400-e29b-41d4-a716-446655440000") == (
        "Contact <EMAIL> for run 550e8400-e29b-41d4-a716-446655440000"
    )


@pytest.mark.parametrize("engine", ["presidio", "unknown"])
def test_removed_or_unknown_engine_requires_explicit_migration(engine):
    with pytest.raises(ValueError, match="pyfly.logging.redaction.engine.*regex"):
        build_redactor(RedactionProperties(engine=engine))
