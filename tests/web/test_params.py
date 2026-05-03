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
"""Tests for request binding types."""

from typing import get_args, get_origin

from pyfly.web.params import Body, Cookie, Header, PathVar, QueryParam


class TestPathVar:
    def test_generic_type(self):
        hint = PathVar[str]
        assert get_origin(hint) is PathVar
        assert get_args(hint) == (str,)

    def test_int_type(self):
        hint = PathVar[int]
        assert get_origin(hint) is PathVar
        assert get_args(hint) == (int,)


class TestQueryParam:
    def test_generic_type(self):
        hint = QueryParam[int]
        assert get_origin(hint) is QueryParam
        assert get_args(hint) == (int,)

    def test_optional_type(self):
        hint = QueryParam[str | None]
        assert get_origin(hint) is QueryParam
        args = get_args(hint)
        assert len(args) == 1


class TestBody:
    def test_generic_type(self):
        from pydantic import BaseModel

        class MyModel(BaseModel):
            name: str

        hint = Body[MyModel]
        assert get_origin(hint) is Body
        assert get_args(hint) == (MyModel,)


class TestHeader:
    def test_generic_type(self):
        hint = Header[str]
        assert get_origin(hint) is Header
        assert get_args(hint) == (str,)


class TestCookie:
    def test_generic_type(self):
        hint = Cookie[str]
        assert get_origin(hint) is Cookie
        assert get_args(hint) == (str,)
