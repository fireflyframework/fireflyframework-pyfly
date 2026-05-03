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
"""Tests for the fallback decorator."""

from __future__ import annotations

import pytest

from pyfly.resilience.fallback import fallback


@pytest.mark.asyncio
async def test_success_bypasses_fallback() -> None:
    """Successful call returns normal result; fallback is never invoked."""

    @fallback(fallback_value="default")
    async def greet(name: str) -> str:
        return f"Hello, {name}"

    assert await greet("Alice") == "Hello, Alice"


@pytest.mark.asyncio
async def test_fallback_method_on_failure() -> None:
    """On exception, fallback_method is called with same args + exc."""
    calls: list[tuple] = []

    def my_fallback(x: int, y: int, *, exc: Exception) -> int:
        calls.append((x, y, exc))
        return -1

    @fallback(fallback_method=my_fallback)
    async def add(x: int, y: int) -> int:
        raise RuntimeError("boom")

    result = await add(3, 4)

    assert result == -1
    assert len(calls) == 1
    assert calls[0][0] == 3
    assert calls[0][1] == 4
    assert isinstance(calls[0][2], RuntimeError)


@pytest.mark.asyncio
async def test_fallback_value_on_failure() -> None:
    """On exception, fallback_value is returned."""

    @fallback(fallback_value=42)
    async def compute() -> int:
        raise ValueError("bad input")

    assert await compute() == 42


@pytest.mark.asyncio
async def test_specific_exception_types() -> None:
    """Only catches specified exception types; others propagate."""

    @fallback(fallback_value="caught", on=(ValueError,))
    async def risky(fail_type: type[Exception]) -> str:
        raise fail_type("error")

    # ValueError should be caught and fallback returned.
    assert await risky(ValueError) == "caught"

    # TypeError is not in `on`, so it should propagate.
    with pytest.raises(TypeError, match="error"):
        await risky(TypeError)


@pytest.mark.asyncio
async def test_fallback_method_receives_exception() -> None:
    """The exc kwarg contains the actual exception instance."""
    captured_exc: list[Exception] = []

    def capture_exc(*, exc: Exception) -> str:
        captured_exc.append(exc)
        return "recovered"

    @fallback(fallback_method=capture_exc)
    async def failing() -> str:
        raise ConnectionError("connection lost")

    result = await failing()

    assert result == "recovered"
    assert len(captured_exc) == 1
    assert isinstance(captured_exc[0], ConnectionError)
    assert str(captured_exc[0]) == "connection lost"


@pytest.mark.asyncio
async def test_async_fallback_method() -> None:
    """Async fallback method is awaited correctly."""

    async def async_fallback(key: str, *, exc: Exception) -> str:
        return f"fallback:{key}"

    @fallback(fallback_method=async_fallback)
    async def lookup(key: str) -> str:
        raise KeyError(key)

    assert await lookup("missing") == "fallback:missing"


def test_validation_requires_fallback() -> None:
    """Raises ValueError if neither fallback_method nor fallback_value provided."""
    with pytest.raises(ValueError, match="Either fallback_method or fallback_value must be provided"):
        fallback()
