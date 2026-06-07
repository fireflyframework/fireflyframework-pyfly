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
"""Runtime, reflection-based type-to-type mapper inspired by MapStruct.

Maps between dataclasses and Pydantic models by matching field names, with
custom renaming, value transformers, field exclusion, computed/projection
fields, and — unlike a flat copy — **recursion into nested models and
collections of models**. It is Pydantic-aware: extraction keeps nested models
as live instances (no ``asdict`` flattening) and construction goes through the
model constructor (validating).

This is the runtime equivalent of MapStruct — there is intentionally no
compile-time codegen, no generated ``*Impl`` classes, and no string expression DSL.

Example::

    mapper = Mapper()
    dto = mapper.map(user_entity, UserDTO)                       # auto name-match + nested recursion
    mapper.add_mapping(User, UserDTO, field_map={"username": "name"})
    mapper.add_mapping(User, UserDTO, transformers={"name": str.upper})

    # Declarative (config lives next to the types):
    @mapping(User, UserDTO, rename={"username": "name"}, transform={"email": str.lower})
    class UserMapper: ...
    dto = default_mapper.map(user, UserDTO)
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Callable
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

S = TypeVar("S")
D = TypeVar("D")


def _is_mappable(tp: Any) -> bool:
    """True if *tp* is a type the mapper can construct/recurse into (dataclass or Pydantic model)."""
    if not isinstance(tp, type):
        return False
    if dataclasses.is_dataclass(tp):
        return True
    return hasattr(tp, "model_fields") and hasattr(tp, "model_validate")  # pydantic v2 BaseModel


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
    """Auto-maps between dataclasses / Pydantic models by matching field names.

    Supports custom field renaming, value transformers, field exclusion,
    projections (computed fields), and recursion into nested models and
    collections of models.
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
        """Register a custom mapping between source and destination types."""
        self._mappings[(source_type, dest_type)] = MappingConfig(
            field_map=field_map or {},
            transformers=transformers or {},
            exclude=exclude or set(),
        )

    def map(self, source: S, dest_type: type[D]) -> D:
        """Map *source* to *dest_type*.

        Strategy: explicit ``field_map`` → identical name → transformer (if any)
        → otherwise recurse into nested models / collections of models when the
        destination field's declared type is itself mappable.
        """
        config = self._mappings.get((type(source), dest_type), MappingConfig())
        dest_fields = self._get_field_names(dest_type)
        dest_types = self._field_types(dest_type)
        source_data = self._extract_fields(source)

        kwargs: dict[str, Any] = {}
        for dest_field in dest_fields:
            if dest_field in config.exclude:
                continue
            source_field = self._resolve_source_field(dest_field, config.field_map)
            if source_field not in source_data:
                continue
            value = source_data[source_field]
            if dest_field in config.transformers:
                value = config.transformers[dest_field](value)  # explicit transform wins
            else:
                value = self._convert(value, dest_types.get(dest_field))
            kwargs[dest_field] = value

        return self._construct(dest_type, kwargs)

    def map_list(self, sources: list[S], dest_type: type[D]) -> list[D]:
        """Map a list of source objects to *dest_type*."""
        return [self.map(s, dest_type) for s in sources]

    def register_projection(
        self,
        source_type: type[S],
        projection_type: type[D],
        *,
        transforms: dict[str, Callable[[Any], Any]] | None = None,
    ) -> None:
        """Register a projection with optional computed-field transforms.

        Transforms are keyed by destination field name; each callable receives
        the *entire source object*.
        """
        self._projections[(source_type, projection_type)] = transforms or {}

    def project(self, source: S, projection_type: type[D]) -> D:
        """Map *source* to a projection type, applying registered transforms."""
        transforms = self._projections.get((type(source), projection_type), {})
        dest_fields = self._get_field_names(projection_type)
        source_data = self._extract_fields(source)

        kwargs: dict[str, Any] = {}
        for field in dest_fields:
            if field in transforms:
                kwargs[field] = transforms[field](source)
            elif field in source_data:
                kwargs[field] = source_data[field]

        return self._construct(projection_type, kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _convert(self, value: Any, field_type: Any) -> Any:
        """Recurse into a nested mappable model or a collection of mappable models.

        Returns *value* unchanged for primitives, already-correct types, or
        non-mappable destinations.
        """
        if value is None or field_type is None:
            return value

        origin = get_origin(field_type)
        args = get_args(field_type)

        # Optional[X] / X | None — unwrap to the single non-None arg.
        if origin is Union or isinstance(field_type, types.UnionType):
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return self._convert(value, non_none[0])
            return value

        # Collection of mappable elements.
        if origin in (list, set, frozenset, tuple) and args and _is_mappable(args[0]):
            if isinstance(value, list | set | frozenset | tuple):
                mapped = [
                    self.map(v, args[0]) if (_is_mappable(type(v)) and not isinstance(v, args[0])) else v for v in value
                ]
                if origin is tuple:
                    return tuple(mapped)
                if origin in (set, frozenset):
                    return set(mapped)
                return mapped
            return value

        # Nested mappable destination.
        if _is_mappable(field_type):
            if isinstance(value, field_type):
                return value
            if _is_mappable(type(value)):
                return self.map(value, field_type)
        return value

    @staticmethod
    def _construct(dest_type: type[D], kwargs: dict[str, Any]) -> D:
        """Construct the destination — Pydantic models validate via their constructor."""
        return dest_type(**kwargs)

    @staticmethod
    def _resolve_source_field(dest_field: str, field_map: dict[str, str]) -> str:
        """Reverse-lookup the source field for a destination field (``field_map`` is source→dest)."""
        for src, dst in field_map.items():
            if dst == dest_field:
                return src
        return dest_field

    @staticmethod
    def _get_field_names(cls: type) -> list[str]:
        """Field names of a type (dataclass, Pydantic model, or typed class)."""
        if dataclasses.is_dataclass(cls):
            return [f.name for f in dataclasses.fields(cls)]
        model_fields = getattr(cls, "model_fields", None)
        if isinstance(model_fields, dict):
            return list(model_fields.keys())
        try:
            return list(get_type_hints(cls).keys())
        except Exception:  # noqa: BLE001 - unresolved forward refs: no fields
            return []

    @staticmethod
    def _field_types(cls: type) -> dict[str, Any]:
        """Declared field types of *cls* (drives nested-model recursion)."""
        try:
            return dict(get_type_hints(cls))
        except Exception:  # noqa: BLE001 - unresolved forward refs: skip recursion
            return {}

    @staticmethod
    def _extract_fields(obj: object) -> dict[str, Any]:
        """Shallow field extraction — keeps nested models as live instances.

        (Deep ``dataclasses.asdict`` would flatten nested models to dicts and
        break nested-destination recursion.)
        """
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        model_fields = getattr(type(obj), "model_fields", None)
        if isinstance(model_fields, dict):  # pydantic v2 — shallow, alias-agnostic
            return {name: getattr(obj, name) for name in model_fields}
        return dict(vars(obj))


# ---------------------------------------------------------------------------
# Declarative layer: a default mapper + a @mapping decorator so a mapping's
# config can live next to the types instead of in imperative add_mapping calls.
# ---------------------------------------------------------------------------

default_mapper = Mapper()


def mapping(
    source_type: type,
    dest_type: type,
    *,
    rename: dict[str, str] | None = None,
    transform: dict[str, Callable[[Any], Any]] | None = None,
    exclude: set[str] | None = None,
) -> Callable[[type], type]:
    """Class decorator that registers a mapping on the module-level ``default_mapper``.

    Keeps the mapping declaration next to the involved types::

        @mapping(UserEntity, UserResponse, rename={"username": "name"}, transform={"email": str.lower})
        class UserMapper: ...

        resp = default_mapper.map(user_entity, UserResponse)
    """

    def decorator(cls: type) -> type:
        default_mapper.add_mapping(
            source_type,
            dest_type,
            field_map=rename or {},
            transformers=transform or {},
            exclude=exclude or set(),
        )
        cls.__pyfly_mapping__ = (source_type, dest_type)  # type: ignore[attr-defined]
        return cls

    return decorator
