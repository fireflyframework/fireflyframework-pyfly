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
"""Generic type-to-type mapper inspired by MapStruct.

Automatically maps between any two types (dataclasses, plain objects, etc.)
by matching field names.  Supports custom field renaming, value transformers,
and field exclusion.

Example::

    mapper = Mapper()
    dto = mapper.map(user_entity, UserDTO)

    # With custom field mapping
    mapper.add_mapping(User, UserDTO, field_map={"username": "name"})
    dto = mapper.map(user, UserDTO)

    # With transformer
    mapper.add_mapping(User, UserDTO, transformers={"name": str.upper})
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, TypeVar, get_type_hints

S = TypeVar("S")
D = TypeVar("D")


@dataclasses.dataclass
class MappingConfig:
    """Configuration for a type mapping.

    Attributes:
        field_map: Maps source field names to destination field names.
        transformers: Functions to transform values, keyed by dest field name.
        exclude: Destination fields to exclude from mapping.
    """

    field_map: dict[str, str] = dataclasses.field(default_factory=dict)
    transformers: dict[str, Callable[[Any], Any]] = dataclasses.field(default_factory=dict)
    exclude: set[str] = dataclasses.field(default_factory=set)


class Mapper:
    """Auto-maps between types by matching field names.

    Supports dataclasses and any object with typed fields.
    Allows custom field mappings and transformers.

    Usage::

        mapper = Mapper()
        dto = mapper.map(user_entity, UserDTO)

        # With custom field mapping
        mapper = Mapper()
        mapper.add_mapping(User, UserDTO, field_map={"username": "name"})
        dto = mapper.map(user, UserDTO)

        # With transformer
        mapper.add_mapping(User, UserDTO, transformers={"name": str.upper})
    """

    def __init__(self) -> None:
        self._mappings: dict[tuple[type, type], MappingConfig] = {}
        self._projections: dict[tuple[type, type], dict[str, Callable[[Any], Any]]] = {}

    def add_mapping(
        self,
        source_type: type[S],
        dest_type: type[D],
        *,
        field_map: dict[str, str] | None = None,
        transformers: dict[str, Callable[[Any], Any]] | None = None,
        exclude: set[str] | None = None,
    ) -> None:
        """Register a custom mapping between source and destination types.

        Args:
            source_type: The source type to map from.
            dest_type: The destination type to map to.
            field_map: Maps source field names to destination field names.
            transformers: Functions to transform field values (keyed by dest
                field name).
            exclude: Destination fields to exclude from mapping.
        """
        self._mappings[(source_type, dest_type)] = MappingConfig(
            field_map=field_map or {},
            transformers=transformers or {},
            exclude=exclude or set(),
        )

    def map(self, source: S, dest_type: type[D]) -> D:
        """Map source object to destination type.

        Field matching strategy:

        1. Check registered ``field_map`` for explicit source -> dest mapping.
        2. Match by identical field name.
        3. Apply transformers if registered.
        4. Skip fields in exclude set.
        """
        config = self._mappings.get(
            (type(source), dest_type),
            MappingConfig(),
        )
        dest_fields = self._get_field_names(dest_type)
        source_data = self._extract_fields(source)

        kwargs: dict[str, object] = {}
        for dest_field in dest_fields:
            if dest_field in config.exclude:
                continue

            # Find the corresponding source field name
            source_field = self._resolve_source_field(dest_field, config.field_map)

            if source_field in source_data:
                value = source_data[source_field]
                # Apply transformer if registered for this dest field
                if dest_field in config.transformers:
                    value = config.transformers[dest_field](value)
                kwargs[dest_field] = value

        return dest_type(**kwargs)

    def map_list(self, sources: list[S], dest_type: type[D]) -> list[D]:
        """Map a list of source objects to destination type."""
        return [self.map(s, dest_type) for s in sources]

    def register_projection(
        self,
        source_type: type[S],
        projection_type: type[D],
        *,
        transforms: dict[str, Callable[[Any], Any]] | None = None,
    ) -> None:
        """Register a projection with optional computed-field transforms.

        Transforms are keyed by destination field name. The callable
        receives the *entire source object* (not just a single field).

        Usage::

            mapper.register_projection(Order, OrderSummary, transforms={
                "total": lambda o: o.quantity * o.unit_price,
            })
        """
        self._projections[(source_type, projection_type)] = transforms or {}

    def project(self, source: S, projection_type: type[D]) -> D:
        """Map source to a projection type, applying registered transforms.

        Falls back to standard field-name matching for fields without
        explicit transforms.
        """
        transforms = self._projections.get((type(source), projection_type), {})
        dest_fields = self._get_field_names(projection_type)
        source_data = self._extract_fields(source)

        kwargs: dict[str, object] = {}
        for field in dest_fields:
            if field in transforms:
                kwargs[field] = transforms[field](source)
            elif field in source_data:
                kwargs[field] = source_data[field]

        return projection_type(**kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_source_field(
        dest_field: str,
        field_map: dict[str, str],
    ) -> str:
        """Find the source field name for a destination field.

        ``field_map`` is ``{source_name: dest_name}``, so we perform a
        reverse lookup.  If no explicit mapping exists the destination
        field name is assumed to be the same as the source field name.
        """
        for src, dst in field_map.items():
            if dst == dest_field:
                return src
        return dest_field

    @staticmethod
    def _get_field_names(cls: type) -> list[str]:
        """Get field names from a type (supports dataclasses and typed dicts)."""
        if dataclasses.is_dataclass(cls):
            return [f.name for f in dataclasses.fields(cls)]
        # Fall back to type hints
        hints = get_type_hints(cls)
        return list(hints.keys())

    @staticmethod
    def _extract_fields(obj: object) -> dict[str, object]:
        """Extract field values from an object."""
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return vars(obj)
