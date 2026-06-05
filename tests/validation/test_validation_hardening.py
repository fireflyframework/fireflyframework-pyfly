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
"""Regression tests for validation hardening (v26.06.14).

- is_valid_amount rejects non-finite floats (inf/NaN) instead of crashing.
- @validator / @validate_input work on sync AND async target functions.
- @validate_input rejects a non-dict, non-model value instead of silently passing.
- the Visa card-scheme pattern only accepts valid Visa lengths.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pyfly.kernel.exceptions import ValidationException
from pyfly.validation.decorators import validate_input, validator
from pyfly.validation.domain import is_valid_amount, is_valid_credit_card


class _Req(BaseModel):
    n: int


class TestIsValidAmount:
    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_is_rejected_not_crashed(self, bad: float) -> None:
        assert is_valid_amount(bad) is False

    def test_normal_amounts(self) -> None:
        assert is_valid_amount(100.0) is True
        assert is_valid_amount(-1) is False
        assert is_valid_amount(0) is False
        assert is_valid_amount(0, allow_zero=True) is True


class TestValidatorDecorator:
    def test_on_sync_function(self) -> None:
        @validator(lambda x: x > 0, message="must be positive")
        def double(x: int) -> int:
            return x * 2

        assert double(3) == 6
        with pytest.raises(ValidationException, match="must be positive"):
            double(-1)

    @pytest.mark.asyncio
    async def test_on_async_function(self) -> None:
        @validator(lambda x: x > 0, message="must be positive")
        async def double(x: int) -> int:
            return x * 2

        assert await double(3) == 6
        with pytest.raises(ValidationException):
            await double(-1)


class TestValidateInputDecorator:
    def test_sync_dict_is_validated(self) -> None:
        @validate_input(_Req, "data")
        def handle(*, data: object) -> object:
            return data

        result = handle(data={"n": 5})
        assert isinstance(result, _Req) and result.n == 5

    def test_non_dict_non_model_value_is_rejected(self) -> None:
        @validate_input(_Req, "data")
        def handle(*, data: object) -> object:
            return data

        with pytest.raises(ValidationException, match="expected _Req or a dict"):
            handle(data="not-a-dict")

    def test_model_instance_passes_through(self) -> None:
        @validate_input(_Req, "data")
        def handle(*, data: object) -> object:
            return data

        req = _Req(n=9)
        assert handle(data=req) is req

    @pytest.mark.asyncio
    async def test_async_target_still_works(self) -> None:
        @validate_input(_Req, "data")
        async def handle(*, data: object) -> object:
            return data

        result = await handle(data={"n": 7})
        assert isinstance(result, _Req) and result.n == 7


class TestVisaPattern:
    def test_valid_16_digit_visa_accepted(self) -> None:
        assert is_valid_credit_card("4111111111111111") is True  # Luhn-valid Visa

    def test_non_card_number_rejected(self) -> None:
        assert is_valid_credit_card("1234567890123456") is False
