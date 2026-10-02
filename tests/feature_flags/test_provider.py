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
"""FireflyFlagProvider: FlagdCore behind the OpenFeature provider interface (spec 4.3, 5)."""

from __future__ import annotations

from typing import Any

from openfeature.event import ProviderEvent, ProviderEventDetails
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import Reason
from openfeature.provider import FeatureProvider

from pyfly.feature_flags.provider import PROVIDER_NAME, FireflyFlagProvider
from tests.feature_flags.support import bool_flag, bound_client


def _document(**flags: dict[str, Any]) -> dict[str, Any]:
    return {"flags": flags}


def test_the_provider_is_named_firefly() -> None:
    assert FireflyFlagProvider().get_metadata().name == PROVIDER_NAME == "firefly"


def test_update_reports_the_changed_keys() -> None:
    provider = FireflyFlagProvider()
    assert provider.update(_document(a=bool_flag(), b=bool_flag("off"))) == ["a", "b"]
    assert provider.update(_document(a=bool_flag(), b=bool_flag("off"))) == []
    assert provider.update(_document(a=bool_flag("off"), b=bool_flag("off"))) == ["a"]
    assert provider.update(_document(a=bool_flag("off"))) == ["b"]


def test_update_emits_configuration_changed_with_the_changed_keys() -> None:
    provider = FireflyFlagProvider()
    seen: list[tuple[ProviderEvent, list[str] | None]] = []

    def on_emit(_: FeatureProvider, event: ProviderEvent, details: ProviderEventDetails) -> None:
        seen.append((event, details.flags_changed))

    provider.attach(on_emit)
    provider.update(_document(a=bool_flag()))
    provider.update(_document(a=bool_flag()))
    assert seen == [(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, ["a"])]


def test_an_unknown_field_next_to_state_does_not_reject_the_document() -> None:
    """Review focus 5: FlagdCore builds Flag(**definition), so one unknown key failed the WHOLE document."""
    provider = FireflyFlagProvider()
    provider.update(_document(typo=bool_flag(description="kill switch", owner="ops"), other=bool_flag("off")))
    with bound_client(provider) as client:
        assert client.get_boolean_details("typo", False).value is True
        assert client.get_boolean_details("other", True).value is False
    assert provider.definition("typo") == bool_flag()


def test_update_does_not_mutate_the_callers_document() -> None:
    document = _document(a=bool_flag(metadata={"owner": "web"}))
    FireflyFlagProvider().update(document)
    assert document == _document(a=bool_flag(metadata={"owner": "web"}))


def test_resolution_and_errors_through_a_client() -> None:
    provider = FireflyFlagProvider()
    provider.update({"flags": {"size": {"state": "ENABLED", "variants": {"s": 10, "l": 50}, "defaultVariant": "l"}}})
    with bound_client(provider) as client:
        found = client.get_integer_details("size", 1)
        missing = client.get_integer_details("nope", 1)
        wrong = client.get_string_details("size", "x")
    assert (found.value, found.variant, found.reason) == (50, "l", Reason.STATIC)
    assert (missing.value, missing.reason, missing.error_code) == (1, Reason.ERROR, ErrorCode.FLAG_NOT_FOUND)
    assert (wrong.value, wrong.error_code) == ("x", ErrorCode.TYPE_MISMATCH)


def test_shutdown_keeps_the_document() -> None:
    provider = FireflyFlagProvider()
    provider.update(_document(a=bool_flag()))
    provider.shutdown()
    assert provider.definition("a") == bool_flag()
    assert provider.definition("missing") is None
    assert provider.document == {"flags": {"a": bool_flag()}}
