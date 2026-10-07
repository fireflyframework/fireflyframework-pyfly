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
"""Shared test-layer vectors exercised through the real registry."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pyfly.feature_flags.events import FeatureFlagsChanged
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from tests.feature_flags.support import CONFORMANCE, StaticSource, recording_publisher

VECTORS: dict[str, Any] = json.loads((CONFORMANCE / "override-vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
async def test_shared_override_vector(case: dict[str, Any]) -> None:
    publisher, seen = recording_publisher()
    registry = FlagRegistry([StaticSource("config", case["config"])], FireflyFlagProvider(), publisher=publisher)
    try:
        await registry.start()
        changed = registry.set_test_overrides(case["overrides"])
        expected = case["expect"]
        flag = registry.effective_flag(case["key"])
        assert VECTORS["version"] == 1
        assert flag is not None
        assert flag.origin == expected["origin"]
        assert list(flag.overrides) == expected["overrides"]
        assert flag.definition == expected["definition"]
        assert [name for name, _ in registry.layers(case["key"])] == [*expected["overrides"], expected["origin"]]
        assert registry.document()["flags"][case["key"]] == expected["definition"]
        assert registry.document(include_test_overrides=False)["flags"].get(case["key"]) == expected["sharedDefinition"]
        assert changed == expected["changedKeys"]
    finally:
        await registry.stop()
    assert seen[-1] == FeatureFlagsChanged(tuple(expected["changedKeys"]), expected["eventOrigin"])
