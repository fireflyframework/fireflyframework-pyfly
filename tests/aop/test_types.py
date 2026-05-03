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
"""Tests for the JoinPoint dataclass."""

from __future__ import annotations

from pyfly.aop.types import JoinPoint


class TestJoinPoint:
    """JoinPoint construction and default values."""

    def test_creation_with_all_fields(self) -> None:
        def _proceed() -> str:
            return "ok"

        target = object()
        exc = ValueError("boom")

        jp = JoinPoint(
            target=target,
            method_name="do_work",
            args=(1, 2),
            kwargs={"key": "val"},
            return_value=42,
            exception=exc,
            proceed=_proceed,
        )

        assert jp.target is target
        assert jp.method_name == "do_work"
        assert jp.args == (1, 2)
        assert jp.kwargs == {"key": "val"}
        assert jp.return_value == 42
        assert jp.exception is exc
        assert jp.proceed is _proceed

    def test_defaults_are_none(self) -> None:
        jp = JoinPoint(
            target=object(),
            method_name="m",
            args=(),
            kwargs={},
        )

        assert jp.return_value is None
        assert jp.exception is None
        assert jp.proceed is None
