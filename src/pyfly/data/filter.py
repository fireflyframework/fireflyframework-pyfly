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
"""Base filter utilities port — shared Query by Example logic for all data adapters.

Subclasses supply adapter-specific factories (``_create_eq``, ``_create_noop``)
while inheriting the shared ``by()``, ``from_dict()``, and ``from_example()``
algorithms.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import Any


class BaseFilterUtils(ABC):
    """Shared Query by Example logic. Subclasses supply adapter-specific factories."""

    @staticmethod
    @abstractmethod
    def _create_eq(field: str, value: Any) -> Any: ...

    @staticmethod
    @abstractmethod
    def _create_noop() -> Any: ...

    @classmethod
    def by(cls, **kwargs: Any) -> Any:
        """Create a specification from keyword arguments (all eq, ANDed)."""
        specs = [cls._create_eq(field, value) for field, value in kwargs.items()]
        return cls._combine_and(specs)

    @classmethod
    def from_dict(cls, filters: dict[str, Any]) -> Any:
        """Create a specification from a dict of field->value pairs (all eq, ANDed).

        ``None`` values are skipped.
        """
        specs = [cls._create_eq(field, value) for field, value in filters.items() if value is not None]
        return cls._combine_and(specs)

    @classmethod
    def from_example(cls, example: Any) -> Any:
        """Create a specification from an example entity/DTO.

        Extracts non-``None`` field values and creates eq filters for each.
        Supports dataclasses and any object with ``__dict__``.
        """
        if dataclasses.is_dataclass(example) and not isinstance(example, type):
            fields = {f.name: getattr(example, f.name) for f in dataclasses.fields(example)}
        else:
            fields = vars(example)

        specs = [cls._create_eq(field, value) for field, value in fields.items() if value is not None]
        return cls._combine_and(specs)

    @classmethod
    def _combine_and(cls, specs: list[Any]) -> Any:
        """AND-combine a list of specs. Returns a no-op if empty."""
        if not specs:
            return cls._create_noop()
        result = specs[0]
        for s in specs[1:]:
            result = result & s
        return result
