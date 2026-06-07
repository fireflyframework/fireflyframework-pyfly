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
"""HTTP message converters (v26.06.28): JSON+XML read/write, q-value content
negotiation, fail-on-unknown, extensible registry, and response wiring."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from pyfly.web.adapters.starlette.response import handle_return_value
from pyfly.web.message_converters import (
    JsonMessageConverter,
    MessageConverter,
    XmlMessageConverter,
    default_message_converters,
    parse_accept,
)


class _Dto(BaseModel):
    name: str
    qty: int


def test_parse_accept_orders_by_qvalue() -> None:
    assert parse_accept("application/json;q=0.8, application/xml;q=0.9") == [
        "application/xml",
        "application/json",
    ]
    assert parse_accept(None) == ["application/json"]
    # equal q preserves header order
    assert parse_accept("application/xml, application/json") == ["application/xml", "application/json"]


def test_json_converter_roundtrip() -> None:
    c = JsonMessageConverter()
    body, content_type = c.write(_Dto(name="a", qty=2))
    assert content_type == "application/json"
    assert json.loads(body) == {"name": "a", "qty": 2}
    assert c.read(body, _Dto) == _Dto(name="a", qty=2)


def test_json_fail_on_unknown() -> None:
    body = b'{"name": "a", "qty": 2, "extra": 1}'
    assert JsonMessageConverter(fail_on_unknown=False).read(body, _Dto) == _Dto(name="a", qty=2)
    with pytest.raises(ValidationError):
        JsonMessageConverter(fail_on_unknown=True).read(body, _Dto)


def test_xml_converter_roundtrip() -> None:
    c = XmlMessageConverter()
    body, content_type = c.write(_Dto(name="a", qty=2))
    assert content_type == "application/xml"
    assert b"<name>a</name>" in body
    assert b"<qty>2</qty>" in body
    assert c.read(body, _Dto) == _Dto(name="a", qty=2)  # XML text coerced back to int


def test_registry_negotiation() -> None:
    reg = default_message_converters()
    assert isinstance(reg.find_writer("application/xml"), XmlMessageConverter)
    assert isinstance(reg.find_writer("application/json"), JsonMessageConverter)
    assert isinstance(reg.find_writer(None), JsonMessageConverter)  # JSON is the default
    assert isinstance(reg.find_writer("application/xml;q=0.9, application/json;q=0.8"), XmlMessageConverter)
    assert isinstance(reg.find_reader("application/xml"), XmlMessageConverter)
    assert isinstance(reg.find_reader("application/json"), JsonMessageConverter)
    assert isinstance(reg.find_reader(None), JsonMessageConverter)


def test_registry_user_converter_takes_priority() -> None:
    class CborConverter(MessageConverter):
        media_types = ("application/cbor",)

        def write(self, value: object) -> tuple[bytes, str]:
            return b"\x00", "application/cbor"

    reg = default_message_converters()
    reg.add(CborConverter())
    assert isinstance(reg.find_writer("application/cbor"), CborConverter)
    assert isinstance(reg.find_writer("application/json"), JsonMessageConverter)  # others still work


def test_handle_return_value_negotiates_format() -> None:
    assert handle_return_value(_Dto(name="a", qty=2), accept="application/json").media_type == "application/json"
    assert handle_return_value(_Dto(name="a", qty=2), accept="application/xml").media_type == "application/xml"
    # q-values: XML preferred
    xml = handle_return_value(_Dto(name="a", qty=2), accept="application/json;q=0.5, application/xml;q=0.9")
    assert xml.media_type == "application/xml"
