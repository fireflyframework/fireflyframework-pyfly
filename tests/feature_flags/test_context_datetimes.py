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
"""Date-times in the evaluation context are evaluated as Unix epoch milliseconds (CONTRACT.md, "Evaluation context")."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

from pyfly.feature_flags.client import FeatureFlags
from pyfly.feature_flags.context import EvaluationContextResolver
from pyfly.feature_flags.provider import DEPTH_LIMIT, EXPANSION_LIMIT, FireflyFlagProvider, _epoch_millis_context
from tests.feature_flags.support import bool_flag, bound_client

AFTER_2025 = 1735689600000
"""2025-01-01T00:00:00Z in epoch milliseconds."""

Evaluate = Callable[[FireflyFlagProvider, dict[str, Any]], FlagEvaluationDetails[bool]]


def _provider(operator: str, variable: str, operand: Any) -> FireflyFlagProvider:
    """A provider whose flag ``matches`` is ``on`` when ``{operator: [{"var": variable}, operand]}`` holds."""
    provider = FireflyFlagProvider()
    targeting = {"if": [{operator: [{"var": variable}, operand]}, "on", None]}
    provider.update({"flags": {"matches": bool_flag("off", targeting=targeting)}})
    return provider


def _through_an_openfeature_client(provider: FireflyFlagProvider, attributes: dict[str, Any]) -> Any:
    with bound_client(provider) as client:
        return client.get_boolean_details("matches", False, EvaluationContext(attributes=attributes))


def _through_the_facade(provider: FireflyFlagProvider, attributes: dict[str, Any]) -> Any:
    with bound_client(provider) as client:
        facade = FeatureFlags(client, EvaluationContextResolver(contributors=[]))
        return facade.details("matches", False, context=attributes)


EVALUATIONS = pytest.mark.parametrize(
    "evaluate", [_through_an_openfeature_client, _through_the_facade], ids=["openfeature-client", "facade"]
)


@pytest.fixture
def host_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Sets the host time zone (``TZ``); restores the environment and the C library's zone even when the test fails."""

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    try:
        yield use
    finally:
        monkeypatch.undo()
        time.tzset()


@EVALUATIONS
def test_an_aware_datetime_is_epoch_milliseconds(evaluate: Evaluate) -> None:
    provider = _provider(">", "signupAt", AFTER_2025)
    after = evaluate(provider, {"signupAt": datetime(2026, 1, 15, tzinfo=UTC)})
    assert (after.value, after.variant, after.reason) == (True, "on", Reason.TARGETING_MATCH)
    before = evaluate(provider, {"signupAt": datetime(2024, 1, 1, tzinfo=UTC)})
    assert (before.value, before.variant, before.reason) == (False, "off", Reason.DEFAULT)


@EVALUATIONS
def test_an_aware_datetime_in_another_offset_is_the_same_instant(evaluate: Evaluate) -> None:
    provider = _provider("==", "t", AFTER_2025)
    plus_one = timezone(timedelta(hours=1))
    assert evaluate(provider, {"t": datetime(2025, 1, 1, 1, 0, tzinfo=plus_one)}).value is True
    assert evaluate(provider, {"t": datetime(2025, 1, 1, 0, 0, tzinfo=plus_one)}).value is False


@EVALUATIONS
def test_the_value_is_a_float_of_milliseconds_with_the_fraction(evaluate: Evaluate) -> None:
    instant = datetime(2026, 1, 15, 12, 30, 45, 250_000, tzinfo=UTC)
    assert evaluate(_provider("==", "t", instant.timestamp() * 1000.0), {"t": instant}).value is True


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Asia/Tokyo"])
@EVALUATIONS
def test_a_naive_datetime_is_read_as_utc_whatever_the_host_zone(
    evaluate: Evaluate, host_zone: Callable[[str], None], zone: str
) -> None:
    host_zone(zone)
    midnight = datetime(2025, 1, 1)
    # the zone is really in effect: the host would read this value as another instant than UTC does
    assert (midnight.timestamp() == midnight.replace(tzinfo=UTC).timestamp()) is (zone == "UTC")
    assert evaluate(_provider("==", "t", AFTER_2025), {"t": midnight}).value is True
    assert evaluate(_provider(">", "t", AFTER_2025), {"t": datetime(2026, 1, 15)}).value is True
    assert evaluate(_provider(">", "t", AFTER_2025), {"t": datetime(2024, 1, 1)}).value is False


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Pacific/Auckland"])
@EVALUATIONS
def test_a_date_is_midnight_utc(evaluate: Evaluate, host_zone: Callable[[str], None], zone: str) -> None:
    host_zone(zone)
    provider = _provider("==", "d", 1767225600000)
    assert evaluate(provider, {"d": date(2026, 1, 1)}).value is True
    assert evaluate(provider, {"d": date(2026, 1, 2)}).value is False


@pytest.mark.parametrize(
    ("path", "attributes"),
    [
        ("user.joined", {"user": {"joined": datetime(2026, 1, 15, tzinfo=UTC)}}),
        ("user.day", {"user": {"day": date(2026, 1, 15)}}),
        ("events.1", {"events": [datetime(2024, 1, 1, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)]}),
        ("a.b.0.c", {"a": {"b": [{"c": datetime(2026, 1, 15, tzinfo=UTC)}]}}),
        ("a.b.0.1.c", {"a": {"b": [[0, {"c": datetime(2026, 1, 15)}]]}}),
    ],
    ids=["dict", "date-in-dict", "list", "dict-list-dict", "naive-in-list-in-list"],
)
@EVALUATIONS
def test_datetimes_nested_in_dicts_and_lists_are_converted(
    evaluate: Evaluate, path: str, attributes: dict[str, Any]
) -> None:
    details = evaluate(_provider(">", path, AFTER_2025), attributes)
    assert (details.value, details.reason) == (True, Reason.TARGETING_MATCH)


async def test_the_asynchronous_facade_converts_too() -> None:
    provider = _provider(">", "user.joined", AFTER_2025)
    with bound_client(provider) as client:
        facade = FeatureFlags(client, EvaluationContextResolver(contributors=[]))
        after = await facade.details_async("matches", False, context={"user": {"joined": datetime(2026, 1, 15)}})
        before = await facade.details_async("matches", False, context={"user": {"joined": date(2024, 1, 1)}})
    assert (after.value, after.reason) == (True, Reason.TARGETING_MATCH)
    assert (before.value, before.reason) == (False, Reason.DEFAULT)


@EVALUATIONS
def test_the_callers_context_is_left_as_it_was(evaluate: Evaluate) -> None:
    attributes: dict[str, Any] = {
        "signupAt": datetime(2026, 1, 15, tzinfo=UTC),
        "day": date(2026, 1, 1),
        "user": {"joined": datetime(2026, 1, 15), "tags": ["a", datetime(2024, 1, 1, tzinfo=UTC)], "n": 1},
        "pair": (datetime(2026, 1, 15), "x"),
    }
    snapshot = copy.deepcopy(attributes)
    assert evaluate(_provider(">", "signupAt", AFTER_2025), attributes).value is True
    assert attributes == snapshot  # a datetime never equals the float it was evaluated as
    assert isinstance(attributes["user"]["joined"], datetime)
    assert isinstance(attributes["user"]["tags"][1], datetime)
    assert isinstance(attributes["pair"], tuple) and isinstance(attributes["pair"][0], datetime)


def test_the_provider_leaves_the_context_object_it_is_given_untouched() -> None:
    context = EvaluationContext(targeting_key="u-1", attributes={"user": {"joined": datetime(2026, 1, 15, tzinfo=UTC)}})
    attributes = context.attributes
    provider = _provider(">", "user.joined", AFTER_2025)
    assert provider.resolve_boolean_details("matches", False, context).value is True
    assert context.attributes is attributes
    assert context.targeting_key == "u-1"
    assert isinstance(context.attributes["user"]["joined"], datetime)


def test_containers_keep_their_type_and_an_unchanged_context_is_not_copied() -> None:
    joined = datetime(2026, 1, 15, tzinfo=UTC)
    plain = EvaluationContext(targeting_key="u-1", attributes={"plan": "pro", "tags": ["a", {"b": (1, 2)}]})
    assert _epoch_millis_context(plain) is plain

    context = EvaluationContext(
        targeting_key="u-1",
        attributes={"pair": (joined, "x"), "tags": ["a", joined], "mapping": MappingProxyType({"j": joined}), "n": 1},
    )
    converted = _epoch_millis_context(context)
    expected = joined.timestamp() * 1000.0
    assert converted is not context and converted.targeting_key == "u-1"
    assert converted.attributes == {
        "pair": (expected, "x"),
        "tags": ["a", expected],
        "mapping": {"j": expected},
        "n": 1,
    }
    assert type(converted.attributes["pair"]) is tuple and type(converted.attributes["tags"]) is list
    assert type(converted.attributes["mapping"]) is dict
    assert context.attributes["pair"] == (joined, "x")


def test_every_resolve_method_converts() -> None:
    provider = FireflyFlagProvider()
    targeting = {"if": [{">": [{"var": "t"}, AFTER_2025]}, "yes", None]}
    variants: dict[str, Any] = {
        "boolean": {"yes": True, "no": False},
        "string": {"yes": "y", "no": "n"},
        "integer": {"yes": 1, "no": 0},
        "float": {"yes": 1.5, "no": 0.5},
        "object": {"yes": {"a": 1}, "no": {"a": 0}},
    }
    provider.update(
        {
            "flags": {
                kind: {"state": "ENABLED", "variants": values, "defaultVariant": "no", "targeting": targeting}
                for kind, values in variants.items()
            }
        }
    )
    context = EvaluationContext(attributes={"t": datetime(2026, 1, 15, tzinfo=UTC)})
    assert provider.resolve_boolean_details("boolean", False, context).value is True
    assert provider.resolve_string_details("string", "", context).value == "y"
    assert provider.resolve_integer_details("integer", 0, context).value == 1
    assert provider.resolve_float_details("float", 0.0, context).value == 1.5
    assert provider.resolve_object_details("object", {}, context).value == {"a": 1}


def test_the_merged_openfeature_context_is_converted() -> None:
    provider = _provider(">", "signupAt", AFTER_2025)
    api.set_evaluation_context(EvaluationContext(attributes={"signupAt": datetime(2026, 1, 15, tzinfo=UTC)}))
    with bound_client(provider) as client:
        assert client.get_boolean_details("matches", False).value is True
        # the invocation context wins over the API-level one in the merge, and is converted just the same
        newer = EvaluationContext(attributes={"signupAt": datetime(2024, 1, 1, tzinfo=UTC)})
        assert client.get_boolean_details("matches", False, newer).value is False


def test_a_string_that_looks_like_a_date_stays_a_string() -> None:
    provider = _provider("==", "s", "2026-01-01T00:00:00Z")
    context = EvaluationContext(attributes={"s": "2026-01-01T00:00:00Z"})
    assert provider.resolve_boolean_details("matches", False, context).value is True


def test_numbers_and_the_rest_are_passed_through() -> None:
    provider = _provider("==", "n", AFTER_2025)
    for value, expected in [(AFTER_2025, True), (float(AFTER_2025), True), (None, False), ("x", False)]:
        context = EvaluationContext(attributes={"n": value})
        assert provider.resolve_boolean_details("matches", False, context).value is expected


def test_a_missing_context_is_evaluated_as_empty() -> None:
    details = _provider("==", "t", 1).resolve_boolean_details("matches", True, None)
    assert (details.value, details.reason, details.error_code) == (False, Reason.DEFAULT, None)


def test_a_context_nested_far_past_the_depth_limit_does_not_exhaust_the_stack() -> None:
    deep: Any = {"joined": datetime(2026, 1, 15, tzinfo=UTC)}
    for _ in range(10_000):
        deep = {"inner": deep}
    context = EvaluationContext(attributes={"plan": "pro", "deep": deep})
    details = _provider("==", "plan", "pro").resolve_boolean_details("matches", False, context)
    assert details.value is True
    assert context.attributes["deep"] is deep


def _datetime_at_level(level: int) -> EvaluationContext:
    """Attributes whose naive date-time sits in the container at nesting *level* (the attributes are level 1)."""
    attributes: dict[str, Any] = {"when": datetime(2026, 1, 15)}
    for _ in range(level - 1):
        attributes = {"in": attributes}
    return EvaluationContext(attributes=attributes)


def test_containers_are_entered_down_to_the_depth_limit_and_no_further() -> None:
    inside = _datetime_at_level(DEPTH_LIMIT)
    assert _epoch_millis_context(inside) is not inside
    path = ".".join(["in"] * (DEPTH_LIMIT - 1) + ["when"])
    assert _provider(">", path, AFTER_2025).resolve_boolean_details("matches", False, inside).value is True
    beyond = _datetime_at_level(DEPTH_LIMIT + 1)
    assert _epoch_millis_context(beyond) is beyond


class _Visited(dict[str, Any]):
    """A dict that counts how often the converter reads it."""

    reads = 0

    def items(self) -> Any:
        type(self).reads += 1
        return super().items()

    def values(self) -> Any:
        type(self).reads += 1
        return super().values()

    def __iter__(self) -> Iterator[str]:
        type(self).reads += 1
        return super().__iter__()


def test_a_shared_structure_is_visited_within_the_value_budget() -> None:
    """Two references per level, 16 levels: 2**16 paths to the datetime. The walk is bounded by the budget."""
    shared: Any = _Visited(when=datetime(2026, 1, 15, tzinfo=UTC))
    for _ in range(16):
        shared = _Visited(left=shared, right=shared)
    context = EvaluationContext(attributes={"plan": "pro", "shared": shared})
    _Visited.reads = 0
    assert _provider("==", "plan", "pro").resolve_boolean_details("matches", False, context).value is True
    assert 0 < _Visited.reads <= EXPANSION_LIMIT


def test_a_cyclic_context_terminates() -> None:
    cycle: dict[str, Any] = {"when": datetime(2026, 1, 15, tzinfo=UTC)}
    cycle["self"] = cycle
    context = EvaluationContext(attributes={"plan": "pro", "cycle": cycle})
    assert _provider("==", "plan", "pro").resolve_boolean_details("matches", False, context).value is True
    assert cycle["self"] is cycle
