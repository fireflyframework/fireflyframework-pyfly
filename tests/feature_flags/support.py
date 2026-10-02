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

import contextlib
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.hook import Hook
from openfeature.provider import FeatureProvider
from openfeature.provider.no_op_provider import NoOpProvider

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
