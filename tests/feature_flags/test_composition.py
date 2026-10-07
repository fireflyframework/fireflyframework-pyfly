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
"""Per-key composition: the highest layer defining a key supplies its whole definition (spec 4.5)."""

from __future__ import annotations

from pyfly.feature_flags.composition import ComposedFlag, Layer, compose
from pyfly.feature_flags.definitions import FlagDocument, parse_document
from tests.feature_flags.support import bool_flag


def _layer(source: str, **flags: dict[str, object]) -> Layer:
    return Layer(source, FlagDocument(flags=dict(flags)))


def test_the_highest_layer_wins_the_whole_definition_and_records_what_it_shadows() -> None:
    composition = compose(
        [
            _layer("config", a=bool_flag("on", metadata={"owner": "web"})),
            _layer("file", a=bool_flag("off")),
            _layer("store", a=bool_flag("on")),
        ]
    )
    assert composition.flags["a"] == ComposedFlag("a", bool_flag("on"), "store", ("config", "file"))


def test_evaluators_and_metadata_merge_per_name() -> None:
    composition = compose(
        [
            Layer("config", FlagDocument(evaluators={"x": {"var": "a"}, "y": {"var": "b"}}, metadata={"m": 1, "n": 1})),
            Layer("http", FlagDocument(evaluators={"y": {"var": "c"}}, metadata={"n": 2})),
        ]
    )
    assert composition.evaluators == {"x": {"var": "a"}, "y": {"var": "c"}}
    assert composition.metadata == {"m": 1, "n": 2}


def test_to_flagd_is_sorted_and_independent() -> None:
    composition = compose([_layer("config", b=bool_flag(), a=bool_flag("off"))])
    document = composition.to_flagd()
    assert list(document["flags"]) == ["a", "b"] and "$evaluators" not in document and "metadata" not in document
    document["flags"]["a"]["state"] = "DISABLED"
    assert composition.flags["a"].definition["state"] == "ENABLED"


def test_no_layers_compose_to_an_empty_set() -> None:
    assert compose([]).to_flagd() == {"flags": {}}


def test_a_parsed_document_with_text_keys_composes_and_serializes() -> None:
    document = parse_document(
        {
            "flags": {"a": bool_flag(metadata={"on": 1, "owner": "web"})},
            "$evaluators": {"on": {"var": "x"}, "beta": {"var": "y"}},
            "metadata": {"on": 1, "name": 2},
        }
    )
    flagd = compose([Layer("config", document)]).to_flagd()
    assert list(flagd["$evaluators"]) == ["beta", "on"]
    assert flagd["metadata"] == {"name": 2, "on": 1}
    assert flagd["flags"]["a"]["metadata"] == {"on": 1, "owner": "web"}
