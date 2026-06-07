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
"""Central JSON layer (v26.06.27): global config, custom-type registry, camelCase,
and the recursive-normalization robustness fix."""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

from pydantic import BaseModel

from pyfly.core.config import Config
from pyfly.web.json import (
    CamelModel,
    JsonProperties,
    JsonSerializers,
    PyFlyJsonSerializer,
    json_properties_from_config,
)


class _Item(BaseModel):
    item_name: str
    qty: int
    note: str | None = None


class _CamelItem(CamelModel):
    item_name: str
    unit_price: float


class _Money:
    def __init__(self, amount: Decimal, ccy: str) -> None:
        self.amount = amount
        self.ccy = ccy


def test_normalizes_single_model_and_list() -> None:
    s = PyFlyJsonSerializer()
    assert s.to_response_data(_Item(item_name="a", qty=1)) == {"item_name": "a", "qty": 1, "note": None}
    assert s.to_response_data([_Item(item_name="a", qty=1), _Item(item_name="b", qty=2)]) == [
        {"item_name": "a", "qty": 1, "note": None},
        {"item_name": "b", "qty": 2, "note": None},
    ]


def test_robustness_dict_mixed_and_datetime_are_json_safe() -> None:
    s = PyFlyJsonSerializer()
    # dict containing a model + a datetime — previously hit json.dumps and raised TypeError
    out = s.to_response_data({"item": _Item(item_name="a", qty=1), "when": datetime.datetime(2026, 6, 7, 12, 0, 0)})
    assert out["item"]["item_name"] == "a"
    assert out["when"] == "2026-06-07T12:00:00"
    # list of dicts (first element not a BaseModel) — previously left un-normalized
    out2 = s.to_response_data([{"x": 1}, {"x": 2}])
    assert out2 == [{"x": 1}, {"x": 2}]
    json.dumps(out)  # both must be fully JSON-safe
    json.dumps(out2)


def test_exclude_none() -> None:
    s = PyFlyJsonSerializer(JsonProperties(exclude_none=True))
    assert s.to_response_data(_Item(item_name="a", qty=1)) == {"item_name": "a", "qty": 1}


def test_camelcase_output() -> None:
    s = PyFlyJsonSerializer(JsonProperties(property_naming_strategy="camelCase"))
    assert s.to_response_data(_CamelItem(item_name="a", unit_price=1.5)) == {"itemName": "a", "unitPrice": 1.5}


def test_custom_type_registry() -> None:
    registry = JsonSerializers()
    registry.register(_Money, encode=lambda m: {"amount": str(m.amount), "ccy": m.ccy})
    s = PyFlyJsonSerializer(JsonProperties(), registry)
    assert s.to_response_data({"price": _Money(Decimal("9.99"), "USD")}) == {"price": {"amount": "9.99", "ccy": "USD"}}


def test_json_properties_from_config() -> None:
    cfg = Config({"pyfly": {"web": {"json": {"property-naming-strategy": "camelCase", "exclude-none": True}}}})
    props = json_properties_from_config(cfg)
    assert props.effective_by_alias() is True
    assert props.exclude_none is True
