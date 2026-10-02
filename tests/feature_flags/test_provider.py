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

import time
from typing import Any

import pytest
from openfeature.evaluation_context import EvaluationContext
from openfeature.event import ProviderEvent, ProviderEventDetails
from openfeature.exception import ErrorCode, TypeMismatchError
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason
from openfeature.provider import FeatureProvider

from pyfly.feature_flags.provider import DEPTH_LIMIT, EXPANSION_LIMIT, PROVIDER_NAME, FireflyFlagProvider
from tests.feature_flags.support import bool_flag, bound_client


def _document(**flags: dict[str, Any]) -> dict[str, Any]:
    return {"flags": flags}


def _segment(name: str) -> dict[str, Any]:
    """A boolean flag that is ``on`` when the evaluator *name* holds."""
    return bool_flag("off", targeting={"if": [{"$ref": name}, "on", "off"]})


def _chain(length: int, leaf: dict[str, Any]) -> dict[str, Any]:
    """Evaluators ``chain-0`` (*leaf*) to ``chain-<length>``, each ``{"and": [{"$ref": <the one before>}]}``."""
    evaluators: dict[str, Any] = {"chain-0": leaf}
    evaluators |= {f"chain-{i}": {"and": [{"$ref": f"chain-{i - 1}"}]} for i in range(1, length + 1)}
    return evaluators


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
    """{"if": [{"in": [{"var": "x"}, [...]]}, "on", "off"]} holds 9 values besides the list's items. Each flag
    reaches them through one resolved reference, which counts one (R-ref-budget) and is replaced by what it names."""
    items = [f"v{i}" for i in range(EXPANSION_LIMIT - 10 + extra)]
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


@pytest.mark.parametrize(("extra", "parse_error"), [(0, False), (1, True)], ids=["at-the-limit", "one-over"])
def test_every_resolved_reference_costs_one_unit_of_the_budget(extra: int, parse_error: bool) -> None:
    """alias-3 -> alias-2 -> alias-1 -> rule: four resolved references, plus the rule's 9 values and its items."""
    items = [f"v{i}" for i in range(EXPANSION_LIMIT - 13 + extra)]
    evaluators: dict[str, Any] = {"rule": {"if": [{"in": [{"var": "x"}, items]}, "on", "off"]}}
    evaluators |= {"alias-1": {"$ref": "rule"}, "alias-2": {"$ref": "alias-1"}, "alias-3": {"$ref": "alias-2"}}
    provider = FireflyFlagProvider()
    provider.update({"flags": {"aliased": bool_flag("off", targeting={"$ref": "alias-3"})}, "$evaluators": evaluators})
    context = EvaluationContext("u", {"x": "v0"})
    assert _parse_error(provider, "aliased", context) is parse_error
    if not parse_error:
        assert _evaluate(provider, "aliased", False, context).value is True


def test_a_long_chain_under_a_fan_out_is_refused_within_the_budget() -> None:
    """The review's probe: 2 000 evaluators that each only name the next, a 14-level fan-out over the end of that
    chain (16 384 references to it), and 20 flags on top. When references to references cost nothing, every one of
    those references walked the whole chain for free: update() blocked the loop for 14 s."""
    evaluators: dict[str, Any] = {"chain-0": {"==": [{"var": "tier"}, "gold"]}}
    evaluators |= {f"chain-{i}": {"$ref": f"chain-{i - 1}"} for i in range(1, 2000)}
    evaluators |= {"fan-0": {"$ref": "chain-1999"}}
    evaluators |= {f"fan-{i}": {"or": [{"$ref": f"fan-{i - 1}"}, {"$ref": f"fan-{i - 1}"}]} for i in range(1, 15)}
    flags = {f"flag-{n}": _segment("fan-14") for n in range(20)}
    provider = FireflyFlagProvider()
    started = time.perf_counter()
    provider.update({"flags": flags, "$evaluators": evaluators})
    assert time.perf_counter() - started < 2.0  # bounded by the budget; it took 14 s before every reference counted
    gold = EvaluationContext("u", {"tier": "gold"})
    assert all(_parse_error(provider, key, gold) for key in flags)


@pytest.mark.parametrize(
    ("leaf", "parse_error"),
    [({"var": ["tier"]}, False), ({"==": [{"var": "tier"}, "gold"]}, True)],
    ids=["at-the-limit", "one-over"],
)
def test_the_nesting_limit_counts_container_levels(leaf: dict[str, Any], parse_error: bool) -> None:
    """{"if": [...]} is level 1, its list 2, and the reference in it puts chain-62 at level 3 (a resolved reference
    adds no level). Each chain step adds two (its object, its list): chain-0 sits at level 127, so its list is level
    128, and the {"var": ...} object inside the second leaf's list is level 129."""
    provider = FireflyFlagProvider()
    provider.update({"flags": {"deep": _segment("chain-62")}, "$evaluators": _chain(62, leaf)})
    gold = EvaluationContext("u", {"tier": "gold"})
    assert DEPTH_LIMIT == 128
    assert _parse_error(provider, "deep", gold) is parse_error
    if not parse_error:
        assert _evaluate(provider, "deep", False, gold).reason == Reason.TARGETING_MATCH


def test_a_chain_too_deep_is_a_parse_error_for_that_flag_only() -> None:
    """400 chained evaluators expand to some 800 levels: past the stack a recursive pass could use, so the limits are
    decided without recursing deeper than the depth limit."""
    evaluators = _chain(400, {"==": [{"var": "tier"}, "gold"]})
    document = {"flags": {"deep": _segment("chain-400"), "shallow": _segment("chain-20"), "plain": bool_flag()}}
    provider = FireflyFlagProvider()
    provider.update({**document, "$evaluators": evaluators})
    gold = EvaluationContext("u", {"tier": "gold"})
    assert _parse_error(provider, "deep", gold)
    assert _evaluate(provider, "shallow", False, gold).value is True
    assert _evaluate(provider, "plain", False).value is True


def test_a_chain_of_references_to_references_adds_no_level() -> None:
    """2 000 evaluators that each only name the next: followed in a loop, never one Python frame per reference."""
    evaluators: dict[str, Any] = {"alias-0": {"==": [{"var": "tier"}, "gold"]}}
    evaluators |= {f"alias-{i}": {"$ref": f"alias-{i - 1}"} for i in range(1, 2001)}
    provider = FireflyFlagProvider()
    provider.update({"flags": {"aliased": _segment("alias-2000")}, "$evaluators": evaluators})
    details = _evaluate(provider, "aliased", False, EvaluationContext("u", {"tier": "gold"}))
    assert (details.value, details.reason) == (True, Reason.TARGETING_MATCH)


def test_a_reference_used_twice_is_counted_and_resolved_twice() -> None:
    """A diamond is not a cycle: an evaluator named in two sibling places expands in both, and counts in both."""
    items = [f"v{i}" for i in range(5000)]  # {"in": [{"var": "x"}, items]} holds 5 005 values
    provider = FireflyFlagProvider()
    provider.update(
        {
            "flags": {
                "both": bool_flag(
                    "off", targeting={"if": [{"and": [{"$ref": "gold"}, {"$ref": "gold"}]}, "on", "off"]}
                ),
                "twice": bool_flag(
                    "off", targeting={"if": [{"or": [{"$ref": "half"}, {"$ref": "half"}]}, "on", "off"]}
                ),
                "once": _segment("half"),
            },
            "$evaluators": {"gold": {"==": [{"var": "tier"}, "gold"]}, "half": {"in": [{"var": "x"}, items]}},
        }
    )
    assert _evaluate(provider, "both", False, EvaluationContext("u", {"tier": "gold"})).value is True
    assert _parse_error(provider, "twice", EvaluationContext("u", {"x": "v1"}))  # 2 x 5 005 + 6 > 10 000
    assert _evaluate(provider, "once", False, EvaluationContext("u", {"x": "v1"})).value is True


# -- object values are copies, a boolean is no number --------------------------------------------------------------

BANNER = {"state": "ENABLED", "variants": {"plain": {"title": "Hi", "tags": ["a", "b"]}}, "defaultVariant": "plain"}
LIST_FLAG = {"state": "ENABLED", "variants": {"few": [{"id": 1}], "none": []}, "defaultVariant": "few"}


def _mutate(value: Any) -> None:
    """Change a key, the nested list and an item of a list value, whatever *value* is."""
    if isinstance(value, dict):
        value["title"] = "changed"
        value["tags"].append("c")
    else:
        value[0]["id"] = 99
        value.append({"id": 2})


@pytest.mark.parametrize(
    ("key", "definition", "original"),
    [
        ("banner", BANNER, {"title": "Hi", "tags": ["a", "b"]}),
        ("rows", LIST_FLAG, [{"id": 1}]),
    ],
)
async def test_every_object_evaluation_returns_a_copy_of_its_own(
    key: str, definition: dict[str, Any], original: Any
) -> None:
    """A caller that mutates the value it got corrupts neither the next evaluation nor the stored definition."""
    provider = FireflyFlagProvider()
    provider.update(_document(**{key: definition}))
    with bound_client(provider) as client:
        _mutate(client.get_object_value(key, {}))
        _mutate(await client.get_object_value_async(key, {}))
        _mutate(provider.resolve_object_details(key, {}).value)
        assert client.get_object_value(key, {}) == original
        assert (await client.get_object_value_async(key, {})) == original
    assert provider.definition(key) == definition


def test_a_float_request_on_a_boolean_flag_is_a_type_mismatch() -> None:
    provider = FireflyFlagProvider()
    provider.update(
        _document(on=bool_flag("on"), ratio={"state": "ENABLED", "variants": {"one": 1}, "defaultVariant": "one"})
    )
    with pytest.raises(TypeMismatchError):
        provider.resolve_float_details("on", 0.5)
    with bound_client(provider) as client:
        details = client.get_float_details("on", 0.5)
        ratio = client.get_float_details("ratio", 0.5)
    assert (details.value, details.variant, details.reason, details.error_code) == (
        0.5,
        None,
        Reason.ERROR,
        ErrorCode.TYPE_MISMATCH,
    )
    assert (ratio.value, type(ratio.value), ratio.variant) == (1.0, float, "one")  # an integer variant still is a float


async def test_an_async_float_request_on_a_boolean_flag_is_a_type_mismatch() -> None:
    provider = FireflyFlagProvider()
    provider.update(_document(on=bool_flag("on")))
    with bound_client(provider) as client:
        details = await client.get_float_details_async("on", 0.5)
    assert (details.value, details.error_code) == (0.5, ErrorCode.TYPE_MISMATCH)


@pytest.mark.parametrize("method", ["resolve_integer_details", "resolve_float_details"])
def test_a_boolean_flag_answering_the_callers_default_is_no_mismatch(method: str) -> None:
    """flagd checks the type of a variant value only: a disabled flag, or targeting that picks no variant and no
    default variant, answers the caller's default whatever the flag's type."""
    provider = FireflyFlagProvider()
    provider.update(
        _document(
            disabled={**bool_flag("on"), "state": "DISABLED"},
            undecided={
                "state": "ENABLED",
                "variants": {"on": True, "off": False},
                "targeting": {"if": [False, "on", None]},
            },
        )
    )
    resolve = getattr(provider, method)
    assert (resolve("disabled", 7).value, resolve("disabled", 7).reason) == (7, Reason.DISABLED)
    assert (resolve("undecided", 7).value, resolve("undecided", 7).reason) == (7, Reason.DEFAULT)


def test_an_integer_request_on_a_boolean_flag_is_a_type_mismatch() -> None:
    """FlagdCore already refuses it (Python's ``True`` is an int); pinned so a FlagdCore upgrade cannot regress it."""
    provider = FireflyFlagProvider()
    provider.update(_document(on=bool_flag("on")))
    with pytest.raises(TypeMismatchError):
        provider.resolve_integer_details("on", 0)
    with bound_client(provider) as client:
        details = client.get_integer_details("on", 3)
    assert (details.value, details.error_code) == (3, ErrorCode.TYPE_MISMATCH)
