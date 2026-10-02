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
"""The Firefly conformance vectors (spec 4.10): identical expectations in PyFly and LaraFly."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from openfeature.evaluation_context import EvaluationContext

from pyfly.feature_flags.composition import Layer, compose
from pyfly.feature_flags.definitions import FlagDefinitionError, expired_keys, normalize_flags, parse_document
from pyfly.feature_flags.provider import FireflyFlagProvider
from tests.feature_flags.support import CONFORMANCE, bound_client

VECTORS: dict[str, Any] = json.loads((CONFORMANCE / "firefly-vectors.json").read_text(encoding="utf-8"))
TODAY = dt.date.fromisoformat(VECTORS["today"])


def _cases(kind: str) -> list[dict[str, Any]]:
    return [case for case in VECTORS["cases"] if case["kind"] == kind]


def test_the_vectors_cover_every_kind() -> None:
    assert VECTORS["version"] == 1
    assert {case["kind"] for case in VECTORS["cases"]} == {
        "normalize",
        "validate",
        "compose",
        "evaluate",
        "expiry",
        "bucket",
    }


@pytest.mark.parametrize("case", _cases("evaluate"), ids=lambda case: case["name"])
def test_evaluate(case: dict[str, Any]) -> None:
    provider = FireflyFlagProvider()
    provider.update(case["document"])
    context = EvaluationContext(targeting_key=case.get("targetingKey"), attributes=case.get("context", {}))
    with bound_client(provider) as client:
        details = getattr(client, f"get_{case['type']}_details")(case["flag"], case["default"], context)
    actual: dict[str, Any] = {"value": details.value, "variant": details.variant, "reason": str(details.reason)}
    if details.error_code is not None:
        actual["errorCode"] = details.error_code.value
    assert actual == {"variant": None, **case["expect"]}


@pytest.mark.parametrize("case", _cases("bucket"), ids=lambda case: case["name"])
def test_bucket(case: dict[str, Any]) -> None:
    """bucketBy = flagKey + targetingKey; the variant of every key is the reference evaluator's."""
    weights: list[list[Any]] = case["weights"]
    assert len(case["keys"]) >= 300 and len(case["keys"]) == len(case["expect"])
    flag = {
        "state": "ENABLED",
        "variants": {variant: variant for variant, _ in weights},
        "defaultVariant": weights[0][0],
        "targeting": {"fractional": weights},
    }
    provider = FireflyFlagProvider()
    provider.update({"flags": {case["flagKey"]: flag}})
    actual = [
        provider.resolve_string_details(case["flagKey"], "fallback", EvaluationContext(targeting_key=key)).value
        for key in case["keys"]
    ]
    assert actual == case["expect"]


@pytest.mark.parametrize("case", _cases("normalize"), ids=lambda case: case["name"])
def test_normalize(case: dict[str, Any]) -> None:
    assert normalize_flags(case["input"]) == case["expect"]


@pytest.mark.parametrize("case", _cases("validate"), ids=lambda case: case["name"])
def test_validate(case: dict[str, Any]) -> None:
    expect = case["expect"]
    if expect["valid"]:
        parse_document(case["input"])
        return
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(case["input"])
    assert raised.value.key == expect["key"]
    assert expect["error"] in str(raised.value)


@pytest.mark.parametrize("case", _cases("compose"), ids=lambda case: case["name"])
def test_compose(case: dict[str, Any]) -> None:
    layers = [
        Layer(
            layer["source"],
            parse_document({"flags": layer["flags"], "$evaluators": layer.get("$evaluators", {})}, shorthand=True),
        )
        for layer in case["layers"]
    ]
    composition = compose(layers)
    actual = {
        key: {"origin": flag.origin, "overrides": list(flag.overrides), "definition": flag.definition}
        for key, flag in composition.flags.items()
    }
    assert actual == case["expect"]["flags"]
    if "evaluators" in case["expect"]:
        assert composition.evaluators == case["expect"]["evaluators"]


@pytest.mark.parametrize("case", _cases("expiry"), ids=lambda case: case["name"])
def test_expiry(case: dict[str, Any]) -> None:
    assert expired_keys(parse_document({"flags": case["flags"]}).flags, TODAY) == case["expect"]["expired"]
