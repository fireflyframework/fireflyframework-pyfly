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
"""Tests for MongoDB query compiler projection support."""

from __future__ import annotations

from typing import Protocol

from pyfly.data.projection import is_projection, projection, projection_fields


@projection
class OrderSummaryProjection(Protocol):
    id: str
    status: str


class TestMongoProjectionDocument:
    def test_build_projection_document(self) -> None:
        fields = projection_fields(OrderSummaryProjection)
        proj_doc = {f: 1 for f in fields}
        assert proj_doc == {"id": 1, "status": 1}

    def test_projection_detected(self) -> None:
        assert is_projection(OrderSummaryProjection) is True

    def test_compiler_accepts_return_type(self) -> None:
        from pyfly.data.document.mongodb.query_compiler import MongoQueryMethodCompiler
        from pyfly.data.query_parser import QueryMethodParser

        parser = QueryMethodParser()
        parsed = parser.parse("find_by_status")
        compiler = MongoQueryMethodCompiler()

        # Should not raise with return_type parameter
        fn = compiler.compile(parsed, object, return_type=list[OrderSummaryProjection])
        assert callable(fn)

    def test_compiler_works_without_return_type(self) -> None:
        from pyfly.data.document.mongodb.query_compiler import MongoQueryMethodCompiler
        from pyfly.data.query_parser import QueryMethodParser

        parser = QueryMethodParser()
        parsed = parser.parse("find_by_status")
        compiler = MongoQueryMethodCompiler()

        fn = compiler.compile(parsed, object)
        assert callable(fn)
