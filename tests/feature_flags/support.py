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
"""Helpers shared by the feature-flag tests."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.hook import Hook
from openfeature.provider import FeatureProvider
from openfeature.provider.no_op_provider import NoOpProvider

from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.sources import SourceSnapshot

CONFORMANCE = Path(__file__).parent / "conformance"


def bool_flag(default: str = "on", **extra: Any) -> dict[str, Any]:
    """A boolean flagd definition (variants ``on``/``off``) whose default variant is *default*."""
    return {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": default, **extra}


@contextlib.contextmanager
def bound_client(provider: FeatureProvider, *, hooks: Sequence[Hook] = ()) -> Iterator[OpenFeatureClient]:
    """*provider* installed under a domain of its own, and a client of that domain."""
    domain = f"test-{uuid.uuid4().hex}"
    api.set_provider_and_wait(provider, domain)
    try:
        yield OpenFeatureClient(domain=domain, version=None, hooks=list(hooks))
    finally:
        api.set_provider_and_wait(NoOpProvider(), domain)


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll *predicate* every 10 ms until it holds; fail the test after *timeout* seconds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        await asyncio.sleep(0.01)


class StaticSource:
    """A source answering one document, then "unchanged" (``None``) on every later load."""

    def __init__(
        self,
        name: str,
        flags: Mapping[str, Any],
        *,
        fail_fast: bool = False,
        refresh_interval: float | None = None,
        evaluators: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.fail_fast = fail_fast
        self.refresh_interval = refresh_interval
        self._raw = {"flags": dict(flags), "$evaluators": dict(evaluators or {})}
        self.loads = 0
        self.closed = False

    async def load(self) -> SourceSnapshot | None:
        self.loads += 1
        return SourceSnapshot(parse_document(self._raw, shorthand=True), "1") if self.loads == 1 else None

    async def close(self) -> None:
        self.closed = True


class ScriptedSource:
    """A source answering, load after load, the next scripted result: a flags mapping, ``None`` (unchanged) or an
    exception to raise. When the script runs out it answers ``None``."""

    def __init__(
        self,
        name: str,
        *results: Mapping[str, Any] | BaseException | None,
        fail_fast: bool = False,
        refresh_interval: float | None = None,
    ) -> None:
        self.name = name
        self.fail_fast = fail_fast
        self.refresh_interval = refresh_interval
        self._results = list(results)
        self.loads = 0

    async def load(self) -> SourceSnapshot | None:
        self.loads += 1
        result = self._results.pop(0) if self._results else None
        if isinstance(result, BaseException):
            raise result
        if result is None:
            return None
        return SourceSnapshot(parse_document({"flags": dict(result)}, shorthand=True), str(self.loads))

    async def close(self) -> None:
        return None
