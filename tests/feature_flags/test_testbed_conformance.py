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
"""flagd-testbed v3.10.2's evaluator suite through FireflyFlagProvider and an OpenFeature client (spec 4.3, D2).

Every scenario runs except those tagged @fractional-v1 (the legacy bucketing the contract does not use).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails

from pyfly.feature_flags.provider import FireflyFlagProvider
from tests.feature_flags.gherkin import Scenario, read_feature
from tests.feature_flags.support import CONFORMANCE, bound_client

EVALUATOR = CONFORMANCE / "testbed" / "evaluator"
DOCUMENT: dict[str, Any] = json.loads((EVALUATOR / "flags" / "testkit-flags.json").read_text(encoding="utf-8"))
SKIPPED_TAG = "@fractional-v1"
ALL = [scenario for path in sorted((EVALUATOR / "gherkin").glob("*.feature")) for scenario in read_feature(path)]
RUN = [scenario for scenario in ALL if SKIPPED_TAG not in scenario.tags]

_FLAG = re.compile(r'an? (\w+)-flag with key "([^"]*)" and a fallback value "(.*)"')
_ATTRIBUTE = re.compile(r'a context containing a key "([^"]*)", with type "([^"]*)" and with value "(.*)"')
_TARGETING_KEY = re.compile(r'a context containing a targeting key with value "([^"]*)"')
_NESTED = re.compile(
    r'a context containing a nested property with outer key "([^"]*)" and inner key "([^"]*)", with value "([^"]*)"'
)
_VALUE = re.compile(r'the resolved details value should be "(.*)"')
_REASON = re.compile(r'the reason should be "([^"]*)"')
_ERROR_CODE = re.compile(r'the error-code should be "([^"]*)"')


def _coerce(kind: str, value: str) -> Any:
    match kind.lower():
        case "boolean":
            return value == "true"
        case "integer":
            return int(value)
        case "float":
            return float(value)
        case "object":
            return json.loads(value.replace('\\"', '"'))
        case _:
            return value


def test_the_suite_has_its_pinned_size() -> None:
    assert len(RUN) == 125
    assert len(ALL) - len(RUN) == 15


@pytest.mark.parametrize("scenario", RUN, ids=[scenario.id for scenario in RUN])
def test_scenario(scenario: Scenario) -> None:
    provider = FireflyFlagProvider()
    provider.update(DOCUMENT)
    flag: tuple[str, str, Any] | None = None
    attributes: dict[str, Any] = {}
    targeting_key: str | None = None
    details: FlagEvaluationDetails[Any] | None = None
    with bound_client(provider) as client:
        for step in scenario.steps:
            text = step.text
            if text == "an evaluator":
                continue
            if match := _FLAG.fullmatch(text):
                flag = (match.group(1).lower(), match.group(2), _coerce(match.group(1), match.group(3)))
            elif match := _ATTRIBUTE.fullmatch(text):
                attributes[match.group(1)] = _coerce(match.group(2), match.group(3))
            elif match := _TARGETING_KEY.fullmatch(text):
                targeting_key = match.group(1)
            elif match := _NESTED.fullmatch(text):
                attributes.setdefault(match.group(1), {})[match.group(2)] = match.group(3)
            elif text == "the flag was evaluated with details":
                assert flag is not None
                kind, key, default = flag
                context = EvaluationContext(targeting_key=targeting_key, attributes=attributes)
                details = getattr(client, f"get_{kind}_details")(key, default, context)
            elif match := _VALUE.fullmatch(text):
                assert details is not None and flag is not None
                assert details.value == _coerce(flag[0], match.group(1))
            elif match := _REASON.fullmatch(text):
                assert details is not None
                assert str(details.reason) == match.group(1)
            elif match := _ERROR_CODE.fullmatch(text):
                assert details is not None and details.error_code is not None
                assert details.error_code.value == match.group(1)
            elif text == "the resolved metadata is empty":
                assert details is not None and not details.flag_metadata
            elif text == "the resolved metadata should contain":
                assert details is not None
                header, *rows = step.table
                for row in rows:
                    cell = dict(zip(header, row, strict=True))
                    assert details.flag_metadata[cell["key"]] == _coerce(cell["metadata_type"], cell["value"])
            else:
                raise AssertionError(f"unknown step: {text}")
