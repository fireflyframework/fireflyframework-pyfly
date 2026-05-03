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
"""Tests for Valid[T] annotation — parameter validation with structured 422 errors."""

import json

import pytest
from pydantic import BaseModel, Field
from starlette.requests import Request

from pyfly.kernel.exceptions import ValidationException
from pyfly.web.adapters.starlette.resolver import ParameterResolver
from pyfly.web.params import Body, QueryParam, Valid

# ── Test models ───────────────────────────────────────────────────────


class CreateUser(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(gt=0)


class SearchFilters(BaseModel):
    q: str
    page: int = 1


# ── Helpers ───────────────────────────────────────────────────────────


def _make_request(
    *,
    method: str = "POST",
    path: str = "/test",
    path_params: dict | None = None,
    query_string: bytes = b"",
    headers: list | None = None,
    body: bytes | None = None,
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "path_params": path_params or {},
        "query_string": query_string,
        "headers": headers or [],
    }
    if body is not None:

        async def receive():
            return {"type": "http.request", "body": body}

        return Request(scope, receive)
    return Request(scope)


# ── Inspection tests ──────────────────────────────────────────────────


class TestValidInspection:
    def test_valid_standalone_detected_as_body(self):
        """Valid[T] standalone should resolve as Body[T] with validate=True."""

        async def handler(self, body: Valid[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        p = resolver.params[0]
        assert p.binding_type is Body
        assert p.inner_type is CreateUser
        assert p.validate is True

    def test_valid_wrapping_body(self):
        """Valid[Body[T]] should resolve as Body[T] with validate=True."""

        async def handler(self, body: Valid[Body[CreateUser]]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        p = resolver.params[0]
        assert p.binding_type is Body
        assert p.inner_type is CreateUser
        assert p.validate is True

    def test_valid_wrapping_query_param(self):
        """Valid[QueryParam[T]] should resolve as QueryParam[T] with validate=True."""

        async def handler(self, page: Valid[QueryParam[int]]):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 1
        p = resolver.params[0]
        assert p.binding_type is QueryParam
        assert p.inner_type is int
        assert p.validate is True

    def test_plain_body_has_validate_false(self):
        """Body[T] (without Valid) should have validate=False."""

        async def handler(self, body: Body[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        assert resolver.params[0].validate is False

    def test_mixed_valid_and_plain(self):
        """Handler with both Valid and plain params resolves correctly."""

        async def handler(self, body: Valid[CreateUser], page: QueryParam[int] = 1):
            pass

        resolver = ParameterResolver(handler)
        assert len(resolver.params) == 2
        assert resolver.params[0].validate is True
        assert resolver.params[1].validate is False


# ── Resolution tests ──────────────────────────────────────────────────


class TestValidResolution:
    @pytest.mark.asyncio
    async def test_valid_standalone_resolves_valid_body(self):
        """Valid[T] with valid JSON body should resolve to a validated model instance."""

        async def handler(self, user: Valid[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(
            body=json.dumps({"name": "Alice", "age": 30}).encode(),
        )
        kwargs = await resolver.resolve(request)
        assert isinstance(kwargs["user"], CreateUser)
        assert kwargs["user"].name == "Alice"
        assert kwargs["user"].age == 30

    @pytest.mark.asyncio
    async def test_valid_standalone_raises_422_on_invalid_body(self):
        """Valid[T] with invalid JSON body should raise ValidationException."""

        async def handler(self, user: Valid[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(
            body=json.dumps({"name": "Alice", "age": -1}).encode(),
        )
        with pytest.raises(ValidationException) as exc_info:
            await resolver.resolve(request)

        assert exc_info.value.code == "VALIDATION_ERROR"
        assert "errors" in exc_info.value.context
        assert any("age" in str(e.get("loc", "")) for e in exc_info.value.context["errors"])

    @pytest.mark.asyncio
    async def test_valid_standalone_raises_422_on_missing_fields(self):
        """Valid[T] with missing required fields should raise ValidationException."""

        async def handler(self, user: Valid[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(body=b"{}")
        with pytest.raises(ValidationException) as exc_info:
            await resolver.resolve(request)

        assert "Validation failed" in str(exc_info.value)
        errors = exc_info.value.context["errors"]
        field_names = [str(e["loc"][-1]) for e in errors]
        assert "name" in field_names
        assert "age" in field_names

    @pytest.mark.asyncio
    async def test_valid_body_wrapper_resolves_valid(self):
        """Valid[Body[T]] with valid JSON body should resolve normally."""

        async def handler(self, user: Valid[Body[CreateUser]]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(
            body=json.dumps({"name": "Bob", "age": 25}).encode(),
        )
        kwargs = await resolver.resolve(request)
        assert isinstance(kwargs["user"], CreateUser)
        assert kwargs["user"].name == "Bob"

    @pytest.mark.asyncio
    async def test_valid_body_wrapper_raises_422_on_invalid(self):
        """Valid[Body[T]] with invalid body should raise ValidationException."""

        async def handler(self, user: Valid[Body[CreateUser]]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(body=b"{}")
        with pytest.raises(ValidationException):
            await resolver.resolve(request)

    @pytest.mark.asyncio
    async def test_valid_query_param_resolves(self):
        """Valid[QueryParam[int]] should resolve the query param normally."""

        async def handler(self, page: Valid[QueryParam[int]]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(method="GET", query_string=b"page=5")
        kwargs = await resolver.resolve(request)
        assert kwargs["page"] == 5

    @pytest.mark.asyncio
    async def test_plain_body_still_validates_pydantic(self):
        """Body[T] (without Valid) should still run Pydantic's model_validate_json.

        Note: without Valid, Pydantic errors propagate as raw ValidationError
        rather than PyFly's ValidationException.
        """
        from pydantic import ValidationError

        async def handler(self, user: Body[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(body=b"{}")
        with pytest.raises(ValidationError):
            await resolver.resolve(request)


# ── Error response format tests ───────────────────────────────────────


class TestValidErrorFormat:
    @pytest.mark.asyncio
    async def test_validation_exception_has_structured_errors(self):
        """ValidationException from Valid[T] should contain structured error details."""

        async def handler(self, user: Valid[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(
            body=json.dumps({"name": "Alice", "age": -5}).encode(),
        )
        with pytest.raises(ValidationException) as exc_info:
            await resolver.resolve(request)

        exc = exc_info.value
        assert exc.code == "VALIDATION_ERROR"
        errors = exc.context["errors"]
        assert isinstance(errors, list)
        assert len(errors) > 0
        # Each error should have loc and msg
        for err in errors:
            assert "loc" in err
            assert "msg" in err

    @pytest.mark.asyncio
    async def test_validation_exception_message_contains_field_details(self):
        """The exception message should list failing fields and their messages."""

        async def handler(self, user: Valid[CreateUser]):
            pass

        resolver = ParameterResolver(handler)
        request = _make_request(body=b'{"age": -1}')
        with pytest.raises(ValidationException) as exc_info:
            await resolver.resolve(request)

        msg = str(exc_info.value)
        assert "Validation failed:" in msg
        assert "name" in msg
