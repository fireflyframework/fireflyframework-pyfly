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
"""A :class:`~pyfly.domain.DomainEvent` has a JSON form every bus can carry.

``occurred_at`` is a ``datetime``: ``json.dumps(dataclasses.asdict(event))``, what the CQRS publisher and the
brokers did with a domain event, raised ``TypeError`` on every bus. :meth:`DomainEvent.to_payload` gives the
event's fields as JSON values (instants in ISO-8601 UTC), and :meth:`DomainEvent.from_payload` reads them back
into the typed event.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest

from pyfly.domain import DomainEvent
from pyfly.domain.domain_event import event_payload, from_json_value, to_json_value


class Carrier(Enum):
    POST = "post"
    COURIER = "courier"


@dataclass(frozen=True)
class Address:
    street: str
    city: str


@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order_id: uuid.UUID = field(default_factory=uuid.uuid4)
    total: Decimal = Decimal("0")
    carrier: Carrier = Carrier.POST
    ship_by: date | None = None
    address: Address | None = None
    lines: tuple[str, ...] = ()
    tags: dict[str, int] = field(default_factory=dict)


def test_the_payload_is_json_and_instants_are_iso_8601_utc() -> None:
    madrid = timezone(timedelta(hours=2))
    event = OrderShipped(
        occurred_at=datetime(2026, 9, 27, 12, 30, 15, 250000, tzinfo=madrid),
        total=Decimal("19.90"),
        carrier=Carrier.COURIER,
        ship_by=date(2026, 10, 1),
        address=Address("Gran Via 1", "Madrid"),
        lines=("sku-1", "sku-2"),
        tags={"priority": 1},
    )

    payload = event.to_payload()

    assert json.loads(json.dumps(payload)) == payload  # plain JSON values only
    assert payload["occurred_at"] == "2026-09-27T10:30:15.250000+00:00"
    assert payload["event_id"] == event.event_id
    assert payload["order_id"] == str(event.order_id)
    assert payload["total"] == "19.90"
    assert payload["carrier"] == "courier"
    assert payload["ship_by"] == "2026-10-01"
    assert payload["address"] == {"street": "Gran Via 1", "city": "Madrid"}
    assert payload["lines"] == ["sku-1", "sku-2"]
    assert payload["tags"] == {"priority": 1}


def test_a_payload_reads_back_into_the_typed_event() -> None:
    event = OrderShipped(
        total=Decimal("5.00"),
        carrier=Carrier.COURIER,
        ship_by=date(2026, 10, 1),
        address=Address("Calle Mayor 3", "Toledo"),
        lines=("a",),
        tags={"n": 2},
    )

    restored = OrderShipped.from_payload(json.loads(json.dumps(event.to_payload())))

    assert restored == event
    assert restored.occurred_at.tzinfo is not None
    assert isinstance(restored.order_id, uuid.UUID)
    assert isinstance(restored.address, Address)


def test_a_naive_instant_is_taken_as_utc() -> None:
    event = OrderShipped(occurred_at=datetime(2026, 1, 2, 3, 4, 5))
    assert event.to_payload()["occurred_at"] == "2026-01-02T03:04:05+00:00"
    assert OrderShipped.from_payload(event.to_payload()).occurred_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_missing_fields_take_their_defaults_and_unknown_keys_are_ignored() -> None:
    restored = OrderShipped.from_payload({"total": "1.5", "added_by_a_later_version": True})
    assert restored.total == Decimal("1.5")
    assert restored.carrier is Carrier.POST
    assert restored.event_id  # a new id, as for an event built without one


def test_a_value_json_cannot_carry_is_refused_with_its_type() -> None:
    @dataclass(frozen=True)
    class Odd(DomainEvent):
        blob: object = field(default_factory=object)

    with pytest.raises(TypeError, match="object"):
        Odd().to_payload()


def test_event_payload_serves_any_event_object() -> None:
    """The CQRS publisher's events need not be DomainEvent subclasses."""

    @dataclass(frozen=True)
    class Plain:
        at: datetime
        amount: Decimal

    class Legacy:
        def __init__(self) -> None:
            self.when = datetime(2026, 1, 1, tzinfo=UTC)

    assert event_payload(Plain(datetime(2026, 5, 1, tzinfo=UTC), Decimal("2"))) == {
        "at": "2026-05-01T00:00:00+00:00",
        "amount": "2",
    }
    assert event_payload(Legacy()) == {"when": "2026-01-01T00:00:00+00:00"}
    assert event_payload(OrderShipped(total=Decimal("3")))["total"] == "3"


def test_an_event_without_a_dict_has_an_empty_payload_and_private_attributes_stay_out() -> None:
    """``vars()`` raised TypeError for an event without ``__dict__`` (the CQRS publisher used to send ``{}``);
    private attributes are not part of the payload."""

    class Slotted:
        __slots__ = ("order_id",)

        def __init__(self) -> None:
            self.order_id = "o-1"

    class WithPrivate:
        def __init__(self) -> None:
            self.order_id = "o-2"
            self._cache = object()  # not JSON, and not the event's

    assert event_payload(Slotted()) == {}
    assert event_payload(WithPrivate()) == {"order_id": "o-2"}


def test_the_codec_functions_round_trip_single_values() -> None:
    instant = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    assert from_json_value(to_json_value(instant), datetime) == instant
    assert from_json_value(to_json_value(Decimal("0.1")), Decimal) == Decimal("0.1")
    assert from_json_value(to_json_value([1, 2]), list[int]) == [1, 2]
    assert from_json_value(None, int | None) is None
