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
"""Tests for exception converter system."""

from pydantic import BaseModel, ValidationError

from pyfly.kernel.exceptions import (
    InvalidRequestException,
    ValidationException,
)
from pyfly.web.converters import (
    ExceptionConverterService,
    JSONExceptionConverter,
    PydanticExceptionConverter,
)


class TestExceptionConverterService:
    def test_converts_with_matching_converter(self):
        service = ExceptionConverterService([PydanticExceptionConverter()])

        class BadModel(BaseModel):
            name: str

        try:
            BadModel.model_validate({})
        except ValidationError as exc:
            result = service.convert(exc)
            assert isinstance(result, ValidationException)
            assert result.code == "VALIDATION_ERROR"

    def test_returns_none_when_no_converter_matches(self):
        service = ExceptionConverterService([])
        result = service.convert(RuntimeError("boom"))
        assert result is None

    def test_first_matching_converter_wins(self):
        service = ExceptionConverterService(
            [
                PydanticExceptionConverter(),
                JSONExceptionConverter(),
            ]
        )
        # JSON decode error
        import json

        try:
            json.loads("{bad json")
        except json.JSONDecodeError as exc:
            result = service.convert(exc)
            assert isinstance(result, InvalidRequestException)

    def test_chain_processes_in_order(self):
        service = ExceptionConverterService(
            [
                JSONExceptionConverter(),
                PydanticExceptionConverter(),
            ]
        )
        import json

        try:
            json.loads("not json")
        except json.JSONDecodeError as exc:
            result = service.convert(exc)
            assert isinstance(result, InvalidRequestException)


class TestPydanticExceptionConverter:
    def test_converts_validation_error(self):
        converter = PydanticExceptionConverter()

        class TestModel(BaseModel):
            email: str
            age: int

        try:
            TestModel.model_validate({"email": 123})
        except ValidationError as exc:
            assert converter.can_handle(exc) is True
            result = converter.convert(exc)
            assert isinstance(result, ValidationException)
            assert "errors" in result.context

    def test_does_not_handle_other_exceptions(self):
        converter = PydanticExceptionConverter()
        assert converter.can_handle(RuntimeError("nope")) is False


class TestJSONExceptionConverter:
    def test_converts_json_decode_error(self):
        converter = JSONExceptionConverter()
        import json

        try:
            json.loads("{bad")
        except json.JSONDecodeError as exc:
            assert converter.can_handle(exc) is True
            result = converter.convert(exc)
            assert isinstance(result, InvalidRequestException)
            assert result.code == "INVALID_JSON"

    def test_does_not_handle_other_exceptions(self):
        converter = JSONExceptionConverter()
        assert converter.can_handle(ValueError("nope")) is False
