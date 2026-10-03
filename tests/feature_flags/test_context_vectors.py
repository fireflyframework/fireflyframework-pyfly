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
"""Shared context vectors exercised through the real resolver and facade."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from openfeature.client import OpenFeatureClient

from pyfly.feature_flags.client import FeatureFlags
from pyfly.feature_flags.context import EvaluationContextResolver
from tests.feature_flags.support import CONFORMANCE

VECTORS: dict[str, Any] = json.loads((CONFORMANCE / "context-vectors.json").read_text(encoding="utf-8"))


class AmbientKey:
    def contribute(self, attributes: dict[str, Any]) -> None:
        attributes["targetingKey"] = "ambient"


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_shared_context_vector(case: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    flags = FeatureFlags(
        OpenFeatureClient(domain="context-vectors", version=None),
        EvaluationContextResolver(contributors=[AmbientKey()]),
    )
    with caplog.at_level(logging.DEBUG, logger="pyfly.feature_flags.context"):
        result = flags.evaluation_context(case["context"])
    assert VECTORS["version"] == 1
    assert result.targeting_key == case["expect"]["targetingKey"]
    assert result.attributes == case["expect"]["attributes"]
    refused = [record for record in caplog.records if record.getMessage() == "feature_flag_targeting_key_refused"]
    assert bool(refused) is case["expect"]["refused"]
    if refused:
        assert len(refused) == 1 and refused[0].source == "context"


def test_refused_key_logging_does_not_expand_the_payload(caplog: pytest.LogCaptureFixture) -> None:
    class Dangerous:
        def __str__(self) -> str:
            raise AssertionError("must not stringify a refused key")

        def __repr__(self) -> str:
            raise AssertionError("must not represent a refused key")

    flags = FeatureFlags(
        OpenFeatureClient(domain="context-vectors", version=None),
        EvaluationContextResolver(contributors=[AmbientKey()]),
    )
    with caplog.at_level(logging.DEBUG, logger="pyfly.feature_flags.context"):
        assert flags.evaluation_context({"targetingKey": Dangerous()}).targeting_key == "ambient"
        assert flags.evaluation_context(targeting_key=Dangerous()).targeting_key == "ambient"
    assert [record.getMessage() for record in caplog.records] == ["feature_flag_targeting_key_refused"] * 2
