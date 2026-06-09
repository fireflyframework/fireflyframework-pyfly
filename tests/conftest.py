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
"""Shared test fixtures for PyFly."""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _force_regex_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the dependency-free regex PII redactor for the whole test suite.

    PII redaction defaults to ``engine='auto'``, which selects the Presidio
    (spaCy NER) engine whenever a spaCy model happens to be installed. Running that
    model inside the asynchronous application-boot path is prone to native-thread
    (BLAS) contention that can stall a single test for tens of seconds — i.e. a test
    that *looks* like an infinite loop — and it needlessly loads a large model on
    every app boot. Tests never need NER-based redaction: the regex engine and the
    Presidio path are each covered directly in ``tests/logging``. So force the fast
    regex engine in the logging adapters, making the suite deterministic and
    hang-free regardless of which spaCy models are installed locally.

    This patches only the adapters' ``build_redactor`` reference, so config/property
    binding (e.g. the documented ``engine='auto'`` default) and the dedicated
    Presidio tests — which call ``build_redactor``/``PresidioRedactor`` directly —
    are unaffected.
    """
    from pyfly.logging.redaction.engine import RegexRedactor

    def _regex_only(props: object):
        if not getattr(props, "enabled", False):
            return None
        return RegexRedactor(props.entities, props.mask, props.extra_patterns)  # type: ignore[attr-defined]

    for module in ("pyfly.logging.stdlib_adapter", "pyfly.logging.structlog_adapter"):
        monkeypatch.setattr(f"{module}.build_redactor", _regex_only, raising=False)
