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
"""Query method compiler port — shared protocol for all data adapters."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Protocol, TypeVar

from pyfly.data.query_parser import ParsedQuery

T = TypeVar("T")


class QueryMethodCompilerPort(Protocol):
    """Compile a ParsedQuery into an async callable.

    Each data adapter (SQLAlchemy, MongoDB, DynamoDB, etc.) provides its own
    implementation. The shared QueryMethodParser produces ParsedQuery objects;
    this port compiles them into backend-specific executable queries.
    """

    def compile(
        self,
        parsed: ParsedQuery,
        entity: type[T],
    ) -> Callable[..., Coroutine[Any, Any, Any]]: ...
