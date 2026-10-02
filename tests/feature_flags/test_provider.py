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

import pytest
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEvent, ProviderEventDetails
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason
from openfeature.provider import FeatureProvider

from pyfly.feature_flags.provider import EXPANSION_LIMIT, PROVIDER_NAME, FireflyFlagProvider
from tests.feature_flags.support import bool_flag, bound_client


def _document(**flags: dict[str, Any]) -> dict[str, Any]:
    return {"flags": flags}


def _segment(name: str) -> dict[str, Any]:
    """A boolean flag that is ``on`` when the evaluator *name* holds."""
    return bool_flag("off", targeting={"if": [{"$ref": name}, "on", "off"]})


def _evaluate(
    provider: FireflyFlagProvider, key: str, default: bool, context: EvaluationContext | None = None
) -> FlagEvaluationDetails[bool]:
    """Through a client: FlagdCore raises ParseError, the SDK turns it into error details."""
    with bound_client(provider) as client:
        return client.get_boolean_details(key, default, context)


def _parse_error(provider: FireflyFlagProvider, key: str, context: EvaluationContext | None = None) -> bool:
    details = _evaluate(provider, key, True, context)
    return details.error_code == ErrorCode.PARSE_ERROR and details.value is True


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


def test_update_signals_the_changed_keys_it_is_given_and_returns_its_own_diff() -> None:
    """The registry's diff is the one change signal (FeatureFlagsChanged and OpenFeature handlers alike)."""
    provider = FireflyFlagProvider()
    seen: list[tuple[ProviderEvent, list[str] | None]] = []

    def on_emit(_: FeatureProvider, event: ProviderEvent, details: ProviderEventDetails) -> None:
        seen.append((event, details.flags_changed))

    provider.attach(on_emit)
    assert provider.update(_document(a=bool_flag(), b=bool_flag()), changed_keys=["b", "a", "doc-only"]) == ["a", "b"]
    assert provider.update(_document(a=bool_flag("off"), b=bool_flag()), changed_keys=[]) == ["a"]
    assert seen == [(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, ["a", "b", "doc-only"])]


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


def test_a_backslash_inside_an_evaluator_is_kept_byte_for_byte() -> None:
    """FlagdCore substitutes $refs with re.sub, the rule's JSON as the template: "a\\d" failed the whole update and
    "C:\\temp" became C:<TAB>emp. References are expanded on the parsed document instead."""
    provider = FireflyFlagProvider()
    provider.update(
        {
            "flags": {"path": _segment("temp-dir"), "digit": _segment("digit")},
            "$evaluators": {
                "temp-dir": {"==": [{"var": "path"}, "C:\\temp\\new"]},
                "digit": {"==": [{"var": "pattern"}, "a\\d"]},
            },
        }
    )
    with bound_client(provider) as client:
        assert client.get_boolean_value("path", False, EvaluationContext("u", {"path": "C:\\temp\\new"})) is True
        corrupted = "C:\temp\new"  # what the textual substitution made of it: a tab and a newline
        assert client.get_boolean_value("path", True, EvaluationContext("u", {"path": corrupted})) is False
        assert client.get_boolean_value("digit", False, EvaluationContext("u", {"pattern": "a\\d"})) is True


def test_references_resolve_transitively_whatever_the_names_sort_as() -> None:
    """FlagdCore makes one pass over the flags in $evaluators order (sorted, as the registry composes them), so it
    never resolved "zz-eu" once "a-staff-in-eu" had brought it in."""
    provider = FireflyFlagProvider()
    provider.update(
        {
            "flags": {"nested": _segment("a-staff-in-eu"), "cyclic": _segment("cycle-a"), "missing": _segment("nope")},
            "$evaluators": {
                "a-staff-in-eu": {"and": [{"in": ["staff", {"var": "roles"}]}, {"$ref": "zz-eu"}]},
                "cycle-a": {"or": [{"$ref": "cycle-b"}, False]},
                "cycle-b": {"or": [{"$ref": "cycle-a"}, False]},
                "eu": {"in": [{"var": "region"}, ["es", "fr"]]},
                "zz-eu": {"$ref": "eu"},
            },
        }
    )
    staff_in_fr = EvaluationContext("u", {"roles": ["staff"], "region": "fr"})
    assert _evaluate(provider, "nested", False, staff_in_fr).value is True
    assert _evaluate(provider, "nested", True, EvaluationContext("u", {"roles": ["staff"]})).value is False
    assert _parse_error(provider, "cyclic") and _parse_error(provider, "missing")


def test_the_document_keeps_the_evaluators_and_the_references_as_written() -> None:
    document = {"flags": {"a": _segment("eu")}, "$evaluators": {"eu": {"in": [{"var": "region"}, ["es"]]}}}
    provider = FireflyFlagProvider()
    provider.update(document)
    assert provider.document == document
    assert provider.definition("a") == _segment("eu")


def test_a_reference_outside_targeting_is_a_plain_value() -> None:
    """Only targeting is expanded (spec 4.1); FlagdCore substituted a matching object anywhere in the flags."""
    provider = FireflyFlagProvider()
    variants = {"ref": {"$ref": "eu"}, "none": {}}
    provider.update(
        {
            "flags": {"obj": {"state": "ENABLED", "variants": variants, "defaultVariant": "ref"}},
            "$evaluators": {"eu": {"in": [{"var": "region"}, ["es"]]}},
        }
    )
    with bound_client(provider) as client:
        assert client.get_object_value("obj", {}) == {"$ref": "eu"}


def test_a_fan_out_of_references_is_a_parse_error_and_never_expanded() -> None:
    """fan-20 doubles at every level: 2**20 references. Counting stops as soon as the budget is spent."""
    evaluators: dict[str, Any] = {"fan-0": {"==": [{"var": "tier"}, "gold"]}}
    evaluators |= {f"fan-{i}": {"or": [{"$ref": f"fan-{i - 1}"}, {"$ref": f"fan-{i - 1}"}]} for i in range(1, 21)}
    provider = FireflyFlagProvider()
    provider.update({"flags": {"huge": _segment("fan-20"), "small": _segment("fan-5")}, "$evaluators": evaluators})
    gold = EvaluationContext("u", {"tier": "gold"})
    assert _parse_error(provider, "huge", gold)
    assert _evaluate(provider, "small", False, gold).value is True


@pytest.mark.parametrize(("extra", "parse_error"), [(0, False), (1, True)], ids=["at-the-limit", "one-over"])
def test_the_expansion_limit_counts_every_json_value(extra: int, parse_error: bool) -> None:
    """{"if": [{"in": [{"var": "x"}, [...]]}, "on", "off"]} holds 9 values besides the list's items; a resolved
    reference counts as what it expands to."""
    items = [f"v{i}" for i in range(EXPANSION_LIMIT - 9 + extra)]
    provider = FireflyFlagProvider()
    provider.update(
        {
            "flags": {"inline": bool_flag("off", targeting={"$ref": "rule"}), "by-ref": _segment("in-list")},
            "$evaluators": {
                "rule": {"if": [{"in": [{"var": "x"}, items]}, "on", "off"]},
                "in-list": {"in": [{"var": "x"}, items]},
            },
        }
    )
    context = EvaluationContext("u", {"x": "v0"})
    assert EXPANSION_LIMIT == 10_000
    for key in ("inline", "by-ref"):
        assert _parse_error(provider, key, context) is parse_error
        if not parse_error:
            assert _evaluate(provider, key, False, context).value is True
