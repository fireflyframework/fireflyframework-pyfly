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
"""Shorthand, validation and expiry beyond the shared vectors (spec 4.1, 4.2)."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from openfeature.evaluation_context import EvaluationContext

from pyfly.feature_flags.composition import Layer, compose
from pyfly.feature_flags.definitions import (
    MAX_DEFINITION_DEPTH,
    FlagDefinitionError,
    FlagDocument,
    expired_keys,
    flag_type,
    is_expired,
    is_valid_key,
    normalize_flags,
    parse_document,
    validate_flag,
)
from pyfly.feature_flags.provider import FireflyFlagProvider
from tests.feature_flags.support import bool_flag, bound_client


def test_shorthand_expands_and_full_definitions_are_copied() -> None:
    full = bool_flag("off", metadata={"owner": "web"})
    normalized = normalize_flags({"a": True, "b": False, "c": "v2", "d": full, 123: True})
    assert normalized["a"]["defaultVariant"] == "on" and normalized["b"]["defaultVariant"] == "off"
    assert normalized["c"] == {"state": "ENABLED", "variants": {"v2": "v2"}, "defaultVariant": "v2"}
    assert normalized["123"] == bool_flag("on")  # YAML reads `123:` as an int key
    normalized["d"]["metadata"]["owner"] = "changed"
    assert full["metadata"]["owner"] == "web"


@pytest.mark.parametrize("key", ["a", "A.b_c-1", "9", "k" * 128])
def test_valid_keys(key: str) -> None:
    assert is_valid_key(key)


@pytest.mark.parametrize("key", ["", "-lead", ".lead", "bad key", "k" * 129, "trailing\n", "über", 7, True])
def test_invalid_keys(key: object) -> None:
    assert not is_valid_key(key)
    with pytest.raises(FlagDefinitionError, match="invalid flag key"):
        validate_flag(key, bool_flag())


def test_a_boolean_and_a_number_do_not_share_a_type() -> None:
    with pytest.raises(FlagDefinitionError, match="variants must share one type"):
        validate_flag("mix", {"state": "ENABLED", "variants": {"a": True, "b": 1}})


def test_integers_and_floats_share_the_number_type() -> None:
    validate_flag("n", {"state": "ENABLED", "variants": {"a": 1, "b": 2.5}, "defaultVariant": "a"})


def test_a_null_metadata_value_is_not_a_scalar() -> None:
    with pytest.raises(FlagDefinitionError, match="metadata values must be scalars"):
        validate_flag("m", bool_flag(metadata={"owner": None}))


def test_unknown_fields_are_ignored_not_rejected() -> None:
    """The contract ignores fields beside the known ones, in a flag and in a document."""
    definition = bool_flag(futureField={"nested": [1, 2]}, note="free text")
    validate_flag("a", definition)
    document = parse_document({"$schema": "https://flagd.dev/schema/v0/flags.json", "flags": {"a": definition}})
    assert set(document.flags) == {"a"}


def test_shorthand_is_refused_where_it_is_not_allowed() -> None:
    with pytest.raises(FlagDefinitionError, match="flag definition must be an object") as raised:
        parse_document({"flags": {"a": True}})
    assert raised.value.key == "a"


PROMO_YAML = """
flags:
  promo:
    state: ENABLED
    variants: {"on": true, "off": false}
    defaultVariant: "on"
    metadata: {expires: 2026-12-31}
"""


def test_a_yaml_date_is_read_as_its_iso_text() -> None:
    """Review focus 1: PyYAML turns an unquoted `expires: 2026-12-31` into a datetime.date."""
    document = parse_document(yaml.safe_load(PROMO_YAML))
    assert document.flags["promo"]["metadata"]["expires"] == "2026-12-31"


def test_a_yaml_timestamp_is_not_a_date() -> None:
    raw = yaml.safe_load(PROMO_YAML.replace("2026-12-31", "2026-12-31T10:00:00Z"))
    with pytest.raises(FlagDefinitionError, match="expires must be a YYYY-MM-DD date"):
        parse_document(raw)


def test_unquoted_yaml_on_off_names_get_a_hint() -> None:
    """YAML 1.1 (PyYAML, pyfly.yaml) reads `on:`/`off:` as booleans; the error says so and keeps the contract phrase."""
    raw = yaml.safe_load(PROMO_YAML.replace('{"on": true, "off": false}', "{on: true, off: false}"))
    with pytest.raises(FlagDefinitionError, match="variants must be a non-empty object") as raised:
        parse_document(raw)
    assert "quote variant names" in str(raised.value)


@pytest.mark.parametrize(
    ("raw", "key", "reason"),
    [
        ([1, 2], "<document>", "document must be an object"),
        ({"flags": [1]}, "flags", "flags must be an object"),
        ({"flags": {"x": 42}}, "x", "flag definition must be an object"),
        (
            {"flags": {}, "$evaluators": {"beta": ["x"]}},
            "$evaluators",
            "targeting must be an object (evaluator 'beta')",
        ),
        ({"flags": {}, "metadata": {"tags": ["a"]}}, "metadata", "metadata values must be scalars"),
    ],
)
def test_document_level_rules(raw: object, key: str, reason: str) -> None:
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(raw)
    assert (raised.value.key, raised.value.reason) == (key, reason)


@pytest.mark.parametrize("section", ["flags", "$evaluators", "metadata"])
def test_an_absent_or_null_section_is_empty(section: str) -> None:
    assert parse_document({}) == FlagDocument()
    assert parse_document({section: None}) == FlagDocument()


@pytest.mark.parametrize(
    ("raw", "key", "reason"),
    [
        ({"flags": False}, "flags", "flags must be an object"),
        ({"flags": 0}, "flags", "flags must be an object"),
        ({"flags": ""}, "flags", "flags must be an object"),
        ({"flags": ["a"]}, "flags", "flags must be an object"),
        ({"$evaluators": False}, "$evaluators", "$evaluators must be an object"),
        ({"$evaluators": 0}, "$evaluators", "$evaluators must be an object"),
        ({"$evaluators": ["a"]}, "$evaluators", "$evaluators must be an object"),
        ({"metadata": False}, "metadata", "metadata must be an object"),
        ({"metadata": ""}, "metadata", "metadata must be an object"),
        ({"metadata": ["a"]}, "metadata", "metadata must be an object"),
    ],
)
def test_a_present_section_that_is_not_an_object_is_rejected_even_when_falsy(
    raw: dict[str, object], key: str, reason: str
) -> None:
    """The error's key is the section name; `flags: false` must not silently load no flags."""
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(raw)
    assert (raised.value.key, raised.value.reason) == (key, reason)


def test_every_section_is_rejected_when_all_are_falsy_non_objects() -> None:
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": False, "$evaluators": 0, "metadata": ""})
    assert raised.value.key == "flags"


def test_unquoted_yaml_on_off_flag_metadata_keys_get_a_hint() -> None:
    raw = yaml.safe_load(PROMO_YAML.replace("{expires: 2026-12-31}", "{on: 1, owner: web}"))
    with pytest.raises(FlagDefinitionError, match="metadata keys must be strings") as raised:
        parse_document(raw)
    assert raised.value.key == "promo"
    assert "quote metadata keys" in str(raised.value)


def test_quoted_yaml_on_off_flag_metadata_keys_are_text_and_valid() -> None:
    raw = yaml.safe_load(PROMO_YAML.replace("{expires: 2026-12-31}", '{"on": 1, "off": 2}'))
    assert parse_document(raw).flags["promo"]["metadata"] == {"on": 1, "off": 2}


def test_unquoted_yaml_document_metadata_keys_are_refused() -> None:
    raw = yaml.safe_load("flags: {}\nmetadata: {on: 1, name: 2}\n")
    with pytest.raises(FlagDefinitionError, match="metadata keys must be strings") as raised:
        parse_document(raw)
    assert raised.value.key == "metadata"
    assert "quote metadata keys" in str(raised.value)


def test_unquoted_yaml_evaluator_names_are_refused_not_renamed() -> None:
    raw = yaml.safe_load("flags: {}\n$evaluators:\n  on: {in: [a, {var: roles}]}\n")
    with pytest.raises(FlagDefinitionError, match="evaluator names must be strings") as raised:
        parse_document(raw)
    assert raised.value.key == "$evaluators"
    assert "quote evaluator names" in str(raised.value)


def test_a_non_text_key_that_is_not_a_boolean_gets_no_yaml_hint() -> None:
    with pytest.raises(FlagDefinitionError, match="metadata keys must be strings") as raised:
        parse_document({"flags": {}, "metadata": {None: 1}})
    assert "YAML" not in str(raised.value)


def test_numeric_yaml_keys_are_text() -> None:
    """`1: x` is an int key in YAML; JSON keys are text, so numeric keys read as their digits."""
    document = parse_document({"flags": {}, "$evaluators": {7: {"var": "a"}}, "metadata": {2: "x"}})
    assert document.evaluators == {"7": {"var": "a"}} and document.metadata == {"2": "x"}


def test_parse_document_returns_plain_flagd() -> None:
    document = parse_document(
        {"flags": {"a": True}, "$evaluators": {"beta": {"in": ["beta", {"var": "roles"}]}}, "metadata": {"v": 1}},
        shorthand=True,
    )
    assert document == FlagDocument(
        flags={"a": bool_flag("on")},
        evaluators={"beta": {"in": ["beta", {"var": "roles"}]}},
        metadata={"v": 1},
    )
    assert document.to_flagd() == {
        "flags": {"a": bool_flag("on")},
        "$evaluators": {"beta": {"in": ["beta", {"var": "roles"}]}},
        "metadata": {"v": 1},
    }


@pytest.mark.parametrize(
    ("variants", "kind"),
    [
        ({"on": True, "off": False}, "boolean"),
        ({"a": "x"}, "string"),
        ({"s": 1, "l": 2.5}, "number"),
        ({"a": {"k": 1}, "b": [1]}, "object"),
    ],
)
def test_flag_type(variants: dict[str, object], kind: str) -> None:
    assert flag_type({"state": "ENABLED", "variants": variants}) == kind


def test_expiry_is_strictly_before_today() -> None:
    today = dt.date(2026, 10, 1)
    flags = {
        "old": bool_flag(metadata={"expires": "2026-09-30"}),
        "today": bool_flag(metadata={"expires": "2026-10-01"}),
        "none": bool_flag(),
    }
    assert expired_keys(flags, today) == ["old"]
    assert not is_expired(flags["today"], today)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('flags: {mk: {state: ENABLED, variants: {"on": true}, metadata: {"": x}}}', "mk"),
        ('flags: {}\nmetadata: {"": x, owner: web}', "metadata"),
    ],
    ids=["flag", "document"],
)
def test_an_empty_metadata_key_is_refused(text: str, key: str) -> None:
    """FlagdCore refuses an empty metadata key (`key must not be empty`) and would refuse the whole document."""
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(yaml.safe_load(text))
    assert (raised.value.key, raised.value.reason) == (key, "metadata keys must not be empty")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("flags: {n: {state: ENABLED, variants: {a: .nan, b: 1.5}, defaultVariant: a}}", "n"),
        ('flags: {t: {state: ENABLED, variants: {"on": true}, targeting: {"<": [{var: x}, .inf]}}}', "t"),
        ('flags: {m: {state: ENABLED, variants: {"on": true}, metadata: {rate: .nan}}}', "m"),
        ('flags: {u: {state: ENABLED, variants: {"on": true}, notes: {weight: -.inf}}}', "u"),
        ("flags: {}\n$evaluators: {big: {'>': [{var: n}, -.inf]}}", "$evaluators"),
        ("flags: {}\nmetadata: {rate: .inf}", "metadata"),
    ],
    ids=["variant", "targeting", "flag-metadata", "unread-field", "evaluator", "document-metadata"],
)
def test_a_number_that_is_not_finite_is_refused(text: str, key: str) -> None:
    """YAML reads `.nan`/`.inf`; JSON has neither, so a definition holding one cannot travel between the frameworks."""
    with pytest.raises(FlagDefinitionError, match="numbers must be finite") as raised:
        parse_document(yaml.safe_load(text))
    assert raised.value.key == key


def test_a_non_finite_number_in_an_evaluator_names_the_evaluator() -> None:
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {}, "$evaluators": {"ok": {"var": "a"}, "big": {">": [{"var": "n"}, float("nan")]}}})
    assert raised.value.key == "$evaluators" and "'big'" in raised.value.reason


def test_finite_floats_and_large_integers_are_numbers_like_any_other() -> None:
    flag = {"state": "ENABLED", "variants": {"a": 0.5, "b": 10**30}, "targeting": {"<": [{"var": "x"}, 1e308]}}
    assert parse_document({"flags": {"f": flag}}).flags["f"] == flag


def _nested(levels: int, bottom: Any = "x") -> dict[str, Any]:
    """A targeting object *levels* containers deep: {"!": [[[...[bottom]...]]]}."""
    node: Any = bottom
    for _ in range(levels - 1):
        node = [node]
    return {"!": node}


def _nested_objects(levels: int, bottom: Any = "x") -> dict[str, Any]:
    """An object *levels* containers deep, objects all the way: {"a": {"a": {...{"a": bottom}...}}}."""
    node: Any = bottom
    for _ in range(levels):
        node = {"a": node}
    return node


def _deep_flag(position: str, levels: int, bottom: Any = "x") -> dict[str, Any]:
    """A flag nesting exactly *levels* levels (the flag object is level 1), the chain sitting in *position*."""
    if position == "targeting":
        return bool_flag(targeting=_nested(levels - 1, bottom))
    if position == "unknown-field":
        return bool_flag(note=_nested(levels - 1, bottom))
    assert position == "variants"  # flag (1) > variants (2) > an object-typed variant value
    return {"state": "ENABLED", "variants": {"a": _nested(levels - 2, bottom), "b": {}}, "defaultVariant": "a"}


POSITIONS = ["targeting", "variants", "unknown-field"]
TOO_DEEP = "definition nests too deeply"


@pytest.mark.parametrize("shorthand", [False, True], ids=["document", "shorthand"])
@pytest.mark.parametrize("position", POSITIONS)
def test_a_flag_may_nest_256_levels_and_not_257(position: str, shorthand: bool) -> None:
    """The flag object is level 1 and each object or array inside adds one, in every field a flag can carry."""
    assert MAX_DEFINITION_DEPTH == 256
    definition = _deep_flag(position, MAX_DEFINITION_DEPTH)
    assert parse_document({"flags": {"deep": definition}}, shorthand=shorthand).flags["deep"] == definition
    validate_flag("deep", definition)
    too_deep = _deep_flag(position, MAX_DEFINITION_DEPTH + 1)
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {"fine": bool_flag(), "deep": too_deep}}, shorthand=shorthand)
    assert (raised.value.key, raised.value.reason) == ("deep", TOO_DEEP)
    with pytest.raises(FlagDefinitionError) as raised:
        validate_flag("deep", too_deep)
    assert (raised.value.key, raised.value.reason) == ("deep", TOO_DEEP)


def test_an_evaluator_rule_may_nest_256_levels_and_not_257() -> None:
    """The rule's own object is level 1, so an evaluator may nest one level deeper than a flag's targeting."""
    rule = _nested(MAX_DEFINITION_DEPTH)
    assert parse_document({"flags": {}, "$evaluators": {"deep": rule}}).evaluators == {"deep": rule}
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {}, "$evaluators": {"fine": {"var": "a"}, "deep": _nested(MAX_DEFINITION_DEPTH + 1)}})
    assert (raised.value.key, raised.value.reason) == ("$evaluators", f"{TOO_DEEP} (evaluator 'deep')")


@pytest.mark.parametrize("shape", [_nested, _nested_objects], ids=["arrays", "objects"])
def test_a_definition_nested_thousands_of_levels_is_refused_not_a_crash(shape: Any) -> None:
    """Nothing recurses: a 5000-level definition is the flag's error, never a RecursionError (a store write answers
    invalid-definition, a source keeps its last good document)."""
    deep = shape(5000)
    for shorthand in (False, True):
        with pytest.raises(FlagDefinitionError) as raised:
            parse_document({"flags": {"deep": bool_flag(targeting=deep), "fine": bool_flag()}}, shorthand=shorthand)
        assert (raised.value.key, raised.value.reason) == ("deep", TOO_DEEP)
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {}, "$evaluators": {"deep": deep}})
    assert (raised.value.key, raised.value.reason) == ("$evaluators", f"{TOO_DEEP} (evaluator 'deep')")
    with pytest.raises(FlagDefinitionError) as raised:
        validate_flag("deep", bool_flag(note=deep))
    assert (raised.value.key, raised.value.reason) == ("deep", TOO_DEEP)


def test_a_document_nested_thousands_of_levels_in_a_section_is_refused_in_its_place() -> None:
    """A deep value in a metadata is no scalar (that rule comes first), and no section is read recursively."""
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {}, "metadata": {"deep": _nested(5000)}})
    assert (raised.value.key, raised.value.reason) == ("metadata", "metadata values must be scalars")
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {"deep": bool_flag(metadata={"deep": _nested(5000)})}})
    assert (raised.value.key, raised.value.reason) == ("deep", "metadata values must be scalars")


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("not_finite", [float("inf"), float("nan")], ids=["inf", "nan"])
def test_depth_is_reported_before_a_number_that_is_not_finite(position: str, not_finite: float) -> None:
    """A value too deep to walk is refused before its numbers are inspected (the contract's reporting order)."""
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {"deep": _deep_flag(position, 5000, bottom=not_finite)}})
    assert raised.value.reason == TOO_DEEP
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {"deep": _deep_flag(position, MAX_DEFINITION_DEPTH + 1, bottom=not_finite)}})
    assert raised.value.reason == TOO_DEEP
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {"deep": bool_flag(note=not_finite, targeting=_nested(5000))}})
    assert raised.value.reason == TOO_DEEP  # the number comes first in the definition, the depth still wins
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {}, "$evaluators": {"deep": _nested(5000, bottom=not_finite)}})
    assert (raised.value.key, raised.value.reason) == ("$evaluators", f"{TOO_DEEP} (evaluator 'deep')")
    # within the limit the number is what is wrong
    with pytest.raises(FlagDefinitionError, match="numbers must be finite"):
        parse_document({"flags": {"deep": _deep_flag(position, MAX_DEFINITION_DEPTH, bottom=not_finite)}})
    with pytest.raises(FlagDefinitionError, match="numbers must be finite"):
        parse_document({"flags": {}, "$evaluators": {"deep": _nested(MAX_DEFINITION_DEPTH, bottom=not_finite)}})


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        pytest.param({"state": "ON"}, "state must be ENABLED or DISABLED", id="state"),
        pytest.param({"variants": {"on": True, "off": 1}}, "variants must share one type", id="variant-types"),
        pytest.param({"targeting": []}, TOO_DEEP, id="empty-targeting-list"),
        pytest.param(
            {"metadata": {"kind": "other"}}, "kind must be one of release, experiment, ops, permission", id="kind"
        ),
    ],
)
def test_depth_comes_after_every_rule_but_the_numbers(broken: dict[str, Any], reason: str) -> None:
    """The first broken rule in the contract's order is the one reported: depth is last, ahead of the numbers only."""
    definition = bool_flag(note=_nested(5000), **broken)
    with pytest.raises(FlagDefinitionError) as raised:
        validate_flag("deep", definition)
    assert raised.value.reason == reason


def test_a_definition_that_contains_itself_is_refused_as_too_deep() -> None:
    """YAML anchors can build a cycle; the walks end at the first container past the limit, however they branch."""
    document = yaml.safe_load(
        "flags:\n  loop:\n    state: ENABLED\n    variants: {a: 1}\n    note: &loop [*loop, *loop]\n"
    )
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(document)
    assert (raised.value.key, raised.value.reason) == ("loop", TOO_DEEP)
    rule = yaml.safe_load("&loop {or: [*loop, *loop]}")
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {}, "$evaluators": {"loop": rule}})
    assert (raised.value.key, raised.value.reason) == ("$evaluators", f"{TOO_DEEP} (evaluator 'loop')")


def test_the_reading_of_a_definition_keeps_its_order_and_gives_dates_and_int_keys_their_text() -> None:
    raw = {
        "flags": {
            "f": bool_flag(
                targeting={"in": [{"var": "day"}, (dt.date(2026, 1, 2), dt.datetime(2026, 1, 2, 3, 4, 5))]},
                note={"z": 1, 2: {"y": [3, {4: "four"}]}, "a": 5},
            ),
            "b": bool_flag(),
            "a": bool_flag(),
        },
        "$evaluators": {"later": {"==": [1, 1]}, "earlier": {"==": [2, 2]}, 7: {"==": [3, 3]}},
        "metadata": {"since": dt.date(2026, 3, 4), "name": "x"},
    }
    document = parse_document(raw)
    assert list(document.flags) == ["f", "b", "a"]
    assert document.flags["f"]["targeting"] == {"in": [{"var": "day"}, ["2026-01-02", "2026-01-02T03:04:05"]]}
    assert document.flags["f"]["note"] == {"z": 1, "2": {"y": [3, {"4": "four"}]}, "a": 5}
    assert list(document.flags["f"]["note"]) == ["z", "2", "a"]
    assert list(document.evaluators) == ["later", "earlier", "7"]
    assert document.metadata == {"since": "2026-03-04", "name": "x"}
    # a copy of its own: the document never shares a container with the input
    assert document.flags["f"]["note"]["2"]["y"] is not raw["flags"]["f"]["note"][2]["y"]
    assert document.flags["f"]["variants"] is not raw["flags"]["f"]["variants"]
    assert document.evaluators["later"] is not raw["$evaluators"]["later"]


def test_of_two_keys_that_read_the_same_the_last_one_stays() -> None:
    document = parse_document({"flags": {"f": bool_flag(note={1: "int", "1": "text"})}})
    assert document.flags["f"]["note"] == {"1": "text"}


def _frames() -> int:
    frame, depth = sys._getframe(), 0
    while frame is not None:
        depth, frame = depth + 1, frame.f_back
    return depth


@contextlib.contextmanager
def _headroom(frames: int) -> Iterator[None]:
    """Run with only *frames* more Python frames than the caller's stack to spare."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(_frames() + frames)
    try:
        yield
    finally:
        sys.setrecursionlimit(limit)


def test_a_definition_at_the_depth_limit_never_exhausts_the_stack_downstream() -> None:
    """Parsing and validation never recurse. The copies made after them (composition, the provider, ``to_flagd``) do,
    two frames a level: 256 levels need about 520 frames, so the limit leaves room in Python's default of 1000."""
    raw = {
        "flags": {f"deep-{position}": _deep_flag(position, MAX_DEFINITION_DEPTH) for position in POSITIONS},
        "$evaluators": {"deep": _nested(MAX_DEFINITION_DEPTH)},
    }
    with _headroom(700):
        document = parse_document(raw)
        flagd = compose([Layer("config", document), Layer("store", document)]).to_flagd()
        assert document.to_flagd() == flagd
        provider = FireflyFlagProvider()
        provider.update(flagd)
        assert provider.document["flags"]["deep-variants"] == flagd["flags"]["deep-variants"]
        assert provider.definition("deep-targeting") is not None
        assert json.loads(json.dumps(flagd)) == flagd


# An empty list stands for an empty object (CONTRACT.md, "Rules every definition must satisfy"): PHP cannot tell `[]`
# from `{}` in native configuration arrays, so both frameworks accept it in the five positions below.

EMPTY_LIST_POSITIONS = [
    pytest.param(
        {"flags": {"p": bool_flag(targeting=[])}},
        FlagDocument(flags={"p": bool_flag(targeting={})}),
        id="flag-targeting",
    ),
    pytest.param(
        {"flags": {"p": bool_flag(metadata=[])}},
        FlagDocument(flags={"p": bool_flag(metadata={})}),
        id="flag-metadata",
    ),
    pytest.param({"flags": []}, FlagDocument(), id="document-flags"),
    pytest.param({"flags": {}, "$evaluators": []}, FlagDocument(), id="document-evaluators"),
    pytest.param({"flags": {}, "metadata": []}, FlagDocument(), id="document-metadata"),
]


@pytest.mark.parametrize("shorthand", [False, True], ids=["document", "shorthand"])
@pytest.mark.parametrize(("raw", "expected"), EMPTY_LIST_POSITIONS)
def test_an_empty_list_is_normalized_to_an_empty_object(
    raw: dict[str, Any], expected: FlagDocument, shorthand: bool
) -> None:
    document = parse_document(raw, shorthand=shorthand)
    assert document == expected  # `[] == {}` is False: the normalized form is pinned, not just accepted
    assert document.to_flagd() == {"flags": expected.flags}


def test_an_empty_list_in_a_flag_is_normalized_in_the_copy_not_in_the_input() -> None:
    raw = {"flags": {"p": bool_flag(targeting=[], metadata=[])}}
    assert parse_document(raw).flags["p"]["targeting"] == {}
    assert raw["flags"]["p"]["targeting"] == [] and raw["flags"]["p"]["metadata"] == []


def test_validate_flag_accepts_an_empty_list_for_targeting_and_metadata() -> None:
    validate_flag("p", bool_flag(targeting=[], metadata=[]))


def test_every_empty_list_position_together_parses_composes_and_evaluates_as_static() -> None:
    document = parse_document(
        {
            "flags": {"el": bool_flag("off", targeting=[], metadata=[])},
            "$evaluators": [],
            "metadata": [],
        }
    )
    assert document.flags["el"]["targeting"] == {} and document.flags["el"]["metadata"] == {}
    assert document.evaluators == {} and document.metadata == {}
    flagd = compose([Layer("config", document)]).to_flagd()
    assert flagd == {"flags": {"el": bool_flag("off", targeting={}, metadata={})}}
    provider = FireflyFlagProvider()
    provider.update(flagd)
    with bound_client(provider) as client:
        details = client.get_boolean_details("el", True, EvaluationContext(targeting_key="user-1"))
    assert (details.value, details.variant, str(details.reason)) == (False, "off", "STATIC")
    assert details.error_code is None


@pytest.mark.parametrize(
    ("raw", "key", "reason"),
    [
        pytest.param(
            {"flags": {"p": bool_flag(targeting=["x"])}}, "p", "targeting must be an object", id="flag-targeting"
        ),
        pytest.param(
            {"flags": {"p": bool_flag(metadata=["x"])}}, "p", "metadata values must be scalars", id="flag-metadata"
        ),
        pytest.param({"flags": ["x"]}, "flags", "flags must be an object", id="document-flags"),
        pytest.param({"$evaluators": ["x"]}, "$evaluators", "$evaluators must be an object", id="document-evaluators"),
        pytest.param({"metadata": ["x"]}, "metadata", "metadata must be an object", id="document-metadata"),
    ],
)
@pytest.mark.parametrize("shorthand", [False, True], ids=["document", "shorthand"])
def test_a_non_empty_list_where_an_object_is_expected_is_still_an_error(
    raw: dict[str, Any], key: str, reason: str, shorthand: bool
) -> None:
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(raw, shorthand=shorthand)
    assert (raised.value.key, raised.value.reason) == (key, reason)


def test_an_empty_list_is_an_object_only_where_the_contract_says_so() -> None:
    """Not a flag definition, not variants and not an evaluator's rule: `[]` is no object there."""
    with pytest.raises(FlagDefinitionError, match="flag definition must be an object"):
        parse_document({"flags": {"p": []}})
    with pytest.raises(FlagDefinitionError, match="variants must be a non-empty object"):
        validate_flag("p", {"state": "ENABLED", "variants": []})
    with pytest.raises(FlagDefinitionError, match="targeting must be an object"):
        parse_document({"flags": {}, "$evaluators": {"beta": []}})


@pytest.mark.parametrize("raw", [[], [1, 2], "flags", 0, 1.5, True, None], ids=repr)
def test_a_document_that_is_not_an_object_is_reported_under_the_document_key(raw: object) -> None:
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document(raw)
    assert (raised.value.key, raised.value.reason) == ("<document>", "document must be an object")
    assert "document must be an object" in str(raised.value)


@pytest.mark.parametrize(
    "variants",
    [{"a": None}, {"a": None, "b": None}, {"a": "x", "b": None}, {"on": True, "off": None}, {"a": 1, "b": None}],
    ids=repr,
)
def test_a_null_variant_value_is_not_a_flag_value(variants: dict[str, object]) -> None:
    with pytest.raises(FlagDefinitionError, match="variants must share one type"):
        validate_flag("nv", {"state": "ENABLED", "variants": variants})
    with pytest.raises(FlagDefinitionError) as raised:
        parse_document({"flags": {"nv": {"state": "ENABLED", "variants": variants}}})
    assert (raised.value.key, raised.value.reason) == ("nv", "variants must share one type")


def test_falsy_variant_values_that_are_not_null_stay_valid() -> None:
    for variants in ({"a": 0}, {"a": ""}, {"a": False}, {"a": {}}, {"a": []}):
        validate_flag("f", {"state": "ENABLED", "variants": variants})
