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

import datetime as dt

import pytest
import yaml

from pyfly.feature_flags.definitions import (
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
from tests.feature_flags.support import bool_flag


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
        ([1, 2], "<document>", "flag definition must be an object"),
        ({"flags": [1]}, "flags", "flags must be an object"),
        ({"flags": {"x": 42}}, "x", "flag definition must be an object"),
        ({"flags": {}, "$evaluators": {"beta": ["x"]}}, "$evaluators.beta", "targeting must be an object"),
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
        ({"flags": []}, "flags", "flags must be an object"),
        ({"$evaluators": False}, "$evaluators", "$evaluators must be an object"),
        ({"$evaluators": 0}, "$evaluators", "$evaluators must be an object"),
        ({"$evaluators": []}, "$evaluators", "$evaluators must be an object"),
        ({"metadata": False}, "metadata", "metadata must be an object"),
        ({"metadata": ""}, "metadata", "metadata must be an object"),
        ({"metadata": []}, "metadata", "metadata must be an object"),
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
        parse_document({"flags": False, "$evaluators": 0, "metadata": []})
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
