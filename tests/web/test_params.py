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
"""Tests for request binding types.

The markers are ``Annotated`` aliases (mypy-transparent: a type checker sees
``PathVar[str]`` as ``str``); the binding source is recovered at runtime from the
annotation metadata via :func:`inspect_binding`.
"""

from typing import get_args

from pydantic import BaseModel

from pyfly.web.params import Body, Cookie, File, Header, PathVar, QueryParam, Valid, inspect_binding


class _Model(BaseModel):
    name: str


class TestUnderlyingType:
    """The underlying (mypy-visible) type is the first ``Annotated`` arg."""

    def test_pathvar_str(self):
        assert get_args(PathVar[str])[0] is str

    def test_pathvar_int(self):
        assert get_args(PathVar[int])[0] is int

    def test_query_optional(self):
        # QueryParam[str | None] keeps the union as the underlying type.
        assert get_args(QueryParam[str | None])[0] == (str | None)


class TestInspectBinding:
    """inspect_binding(hint) -> (binding_alias | None, inner_type, validate)."""

    def test_pathvar(self):
        binding, inner, validate = inspect_binding(PathVar[str])
        assert binding is PathVar
        assert inner is str
        assert validate is False

    def test_queryparam(self):
        binding, inner, validate = inspect_binding(QueryParam[int])
        assert binding is QueryParam
        assert inner is int
        assert validate is False

    def test_body(self):
        binding, inner, validate = inspect_binding(Body[_Model])
        assert binding is Body
        assert inner is _Model
        assert validate is False

    def test_header(self):
        binding, inner, _ = inspect_binding(Header[str])
        assert binding is Header
        assert inner is str

    def test_cookie(self):
        binding, inner, _ = inspect_binding(Cookie[str])
        assert binding is Cookie
        assert inner is str

    def test_file(self):
        binding, _inner, validate = inspect_binding(File[str])
        assert binding is File
        assert validate is False

    def test_valid_wrapping_body(self):
        binding, inner, validate = inspect_binding(Valid[Body[_Model]])
        assert binding is Body
        assert inner is _Model
        assert validate is True

    def test_valid_wrapping_queryparam(self):
        binding, inner, validate = inspect_binding(Valid[QueryParam[int]])
        assert binding is QueryParam
        assert inner is int
        assert validate is True

    def test_valid_standalone_implies_body(self):
        # Valid[Model] with no binding marker resolves to a validated body.
        binding, inner, validate = inspect_binding(Valid[_Model])
        assert binding is Body
        assert inner is _Model
        assert validate is True

    def test_plain_type_has_no_binding(self):
        binding, inner, validate = inspect_binding(str)
        assert binding is None
        assert inner is str
        assert validate is False
