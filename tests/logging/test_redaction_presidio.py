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
"""End-to-end tests for the Presidio PII redaction path.

Skipped unless the optional ``pyfly[pii]`` extra (Presidio) AND a spaCy model are
installed — the dedicated ``PII / Presidio`` CI job installs both. Locally:
``uv pip install presidio-analyzer presidio-anonymizer && python -m spacy download
en_core_web_sm``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("presidio_analyzer")
pytest.importorskip("presidio_anonymizer")

from pyfly.config.properties.logging import PresidioProperties, RedactionProperties  # noqa: E402
from pyfly.logging.redaction.engine import PresidioRedactor, build_redactor  # noqa: E402

_MODEL = "en_core_web_sm"  # tiny model — keeps CI fast


@pytest.fixture
def presidio_redactor() -> PresidioRedactor:
    props = RedactionProperties(engine="presidio", presidio=PresidioProperties(model=_MODEL))
    try:
        return PresidioRedactor(props)
    except Exception as exc:  # noqa: BLE001 — model not downloaded -> skip, don't fail
        pytest.skip(f"presidio spaCy model '{_MODEL}' unavailable: {exc}")


def test_presidio_masks_person_and_email(presidio_redactor: PresidioRedactor) -> None:
    # PERSON detection is the value-add over the regex engine (NER, no fixed pattern).
    out = presidio_redactor.redact("My name is John Smith and my email is john.smith@acme.io")
    assert "John Smith" not in out
    assert "john.smith@acme.io" not in out


def test_presidio_plus_regex_masks_card_and_token(presidio_redactor: PresidioRedactor) -> None:
    out = presidio_redactor.redact("card 4111111111111111 and jwt eyJabc123.dEf456.GhIjk789")
    assert "4111111111111111" not in out
    assert "eyJabc123.dEf456.GhIjk789" not in out


def test_build_redactor_selects_presidio_when_model_present(presidio_redactor: PresidioRedactor) -> None:
    # Reaching here means the model loaded (the fixture didn't skip), so build_redactor
    # must choose Presidio rather than falling back to regex.
    props = RedactionProperties(engine="presidio", presidio=PresidioProperties(model=_MODEL))
    assert type(build_redactor(props)).__name__ == "PresidioRedactor"


def test_auto_engine_selects_presidio_when_available(presidio_redactor: PresidioRedactor) -> None:
    props = RedactionProperties(engine="auto", presidio=PresidioProperties(model=_MODEL))
    assert type(build_redactor(props)).__name__ == "PresidioRedactor"
