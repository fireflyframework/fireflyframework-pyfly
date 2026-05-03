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
"""Tests for validation module: Pydantic helpers and custom validators."""

import pytest
from pydantic import BaseModel

from pyfly.kernel.exceptions import ValidationException
from pyfly.validation.decorators import validate_input, validator
from pyfly.validation.helpers import validate_model


class CreateUserRequest(BaseModel):
    name: str
    email: str
    age: int


class TestValidateModel:
    def test_valid_data(self):
        data = {"name": "Alice", "email": "alice@example.com", "age": 30}
        result = validate_model(CreateUserRequest, data)
        assert result.name == "Alice"
        assert result.email == "alice@example.com"

    def test_invalid_data_raises_validation_exception(self):
        data = {"name": "Alice", "email": "alice@example.com", "age": "not-a-number"}
        with pytest.raises(ValidationException) as exc_info:
            validate_model(CreateUserRequest, data)
        assert exc_info.value.code == "VALIDATION_ERROR"

    def test_missing_required_field(self):
        data = {"name": "Alice"}
        with pytest.raises(ValidationException):
            validate_model(CreateUserRequest, data)


class TestValidateInputDecorator:
    @pytest.mark.asyncio
    async def test_validates_kwargs(self):
        @validate_input(model=CreateUserRequest, param="data")
        async def create_user(data: CreateUserRequest) -> dict:
            return {"name": data.name}

        result = await create_user(data={"name": "Bob", "email": "bob@test.com", "age": 25})
        assert result["name"] == "Bob"

    @pytest.mark.asyncio
    async def test_rejects_invalid_input(self):
        @validate_input(model=CreateUserRequest, param="data")
        async def create_user(data: CreateUserRequest) -> dict:
            return {"name": data.name}

        with pytest.raises(ValidationException):
            await create_user(data={"name": "Bob"})  # Missing required fields


class TestCustomValidator:
    @pytest.mark.asyncio
    async def test_validator_passes(self):
        @validator(lambda x: x > 0, message="Must be positive")
        async def process(value: int) -> int:
            return value * 2

        result = await process(5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_validator_fails(self):
        @validator(lambda x: x > 0, message="Must be positive")
        async def process(value: int) -> int:
            return value * 2

        with pytest.raises(ValidationException, match="Must be positive"):
            await process(-1)

    @pytest.mark.asyncio
    async def test_validator_with_multiple_args(self):
        @validator(lambda start, end: start < end, message="Start must be before end")
        async def create_range(start: int, end: int) -> range:
            return range(start, end)

        result = await create_range(1, 10)
        assert list(result) == list(range(1, 10))

        with pytest.raises(ValidationException, match="Start must be before end"):
            await create_range(10, 1)
