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
"""Composable query predicate port — abstract base for all data adapters.

Each data adapter provides a concrete ``Specification`` subclass that
implements the combinators (``&``, ``|``, ``~``) and ``to_predicate``
using the backend-specific query representation.

Type Parameters:
    T: The entity type.
    Q: The backend query representation (e.g., ``sqlalchemy.Select``, ``dict``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")
Q = TypeVar("Q")


class Specification(ABC, Generic[T, Q]):
    """Composable query predicate port — abstract base for all data adapters.

    Type Parameters:
        T: The entity type.
        Q: The backend query representation (e.g., ``sqlalchemy.Select``, ``dict``).
    """

    @abstractmethod
    def to_predicate(self, root: type[T], query: Q) -> Q: ...

    @abstractmethod
    def __and__(self, other: Specification[T, Q]) -> Specification[T, Q]: ...

    @abstractmethod
    def __or__(self, other: Specification[T, Q]) -> Specification[T, Q]: ...

    @abstractmethod
    def __invert__(self) -> Specification[T, Q]: ...
