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
"""How a Beanie document's Python names map to MongoDB's, and how its ids are typed.

A document's properties are its pydantic fields. The field ``id`` is stored as ``_id``, and a field with an
alias is stored under the alias: sorts, filters, specifications and derived queries name the Python field,
and :func:`field_path` gives the name MongoDB knows (C040). This module registers the document introspector
of :class:`~pyfly.data.property_resolver.PropertyResolver`, so a sort, a filter or a query-by-example on a
document validates its names like one on a relational entity: an unknown name raises
:class:`~pyfly.data.property_resolver.InvalidPropertyError` instead of matching nothing, or everything.

Ids are coerced to the document's id type before they reach a filter (:func:`coerce_id`), so
``MongoRepository[Doc, str]`` finds a document whose ``_id`` is an ``ObjectId`` by its string (C038), and a
new document gets its id on the client when its type allows it (:func:`new_id`): an ``ObjectId``, its hex
string for a ``str`` id, a random ``UUID``.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Mapping
from typing import Any

from beanie import Document, PydanticObjectId
from beanie.odm.utils.encoder import Encoder
from bson import ObjectId
from pydantic import TypeAdapter, ValidationError

from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver, register_property_introspector

ID_FIELD = "_id"
"""The name MongoDB stores a document's ``id`` under."""


def document_properties(entity: type) -> Mapping[str, str] | None:
    """The property introspector of Beanie documents: each field by its Python name, mapped to the name it is
    stored under (``id`` to ``_id``, an aliased field to its alias); ``None`` for any other class."""
    if not (isinstance(entity, type) and issubclass(entity, Document)):
        return None
    properties: dict[str, str] = {}
    for name, info in entity.model_fields.items():
        if name == "id":
            properties[name] = ID_FIELD
        else:
            properties[name] = info.alias or name
    return properties


register_property_introspector(document_properties)


def field_path(entity: type, name: str, *, usage: str = "property", resolver: PropertyResolver | None = None) -> str:
    """The stored name of *name* on *entity*: the first segment of a dotted path is resolved (validated, and
    mapped to ``_id`` or its alias), the rest is kept as written (a field of an embedded document).

    A name that is not a field of *entity* raises :class:`~pyfly.data.property_resolver.InvalidPropertyError`
    naming *usage*; so does a name with ``$`` (an operator). For a class that is not a document the name is
    returned as it is.
    """
    if resolver is None:
        if not (isinstance(entity, type) and issubclass(entity, Document)):
            return name
        resolver = PropertyResolver.for_entity(entity)
    head, dot, rest = str(name).partition(".")
    if "$" in rest:
        raise InvalidPropertyError(
            f"{usage.capitalize()} property {name!r} of {resolver.entity.__name__} is not allowed: property names may "
            "not contain '$' (a query operator)",
            entity=resolver.entity.__name__,
            property=str(name),
            usage=usage,
        )
    return resolver.resolve(head, usage=usage) + dot + rest


def id_type(entity: type) -> Any:
    """The annotation of *entity*'s ``id`` field (``PydanticObjectId | None`` for a default Beanie document)."""
    field = entity.model_fields.get("id")  # type: ignore[attr-defined]
    return field.annotation if field is not None else Any


def _id_class(entity: type) -> type | None:
    annotation = id_type(entity)
    candidates = getattr(annotation, "__args__", None) or (annotation,)
    for candidate in candidates:
        if isinstance(candidate, type) and candidate is not type(None):
            return candidate
    return None


def coerce_id(entity: type, value: Any) -> Any:
    """*value* converted to *entity*'s id type (a string to an ``ObjectId``...), as a document stores it.

    Raises :class:`InvalidIdError` (a 400) for a value the id type does not accept: no document can have it.
    """
    if value is None:
        return None
    id_class = _id_class(entity)
    if id_class is None or isinstance(value, id_class):
        return value
    try:
        return _id_adapter(id_type(entity)).validate_python(value)
    except (ValidationError, TypeError, ValueError) as error:
        raise InvalidIdError(entity, value) from error


@functools.lru_cache(maxsize=256)
def _cached_adapter(annotation: Any) -> TypeAdapter[Any]:
    return TypeAdapter(annotation)


def _id_adapter(annotation: Any) -> TypeAdapter[Any]:
    """The validator of an id annotation, built once per annotation (building one costs more than ten
    validations, and ``find_all_by_id`` converts every id it is given)."""
    try:
        return _cached_adapter(annotation)
    except TypeError:  # an annotation that cannot be hashed (metadata that is not hashable)
        return TypeAdapter(annotation)


def new_id(entity: type) -> Any:
    """An id for a new document of *entity* made on the client, or ``None`` when its id type has no natural
    generator (the server then assigns an ``ObjectId``)."""
    id_class = _id_class(entity)
    if id_class is None:
        return None
    if issubclass(id_class, ObjectId):
        return PydanticObjectId()
    if issubclass(id_class, uuid.UUID):
        return uuid.uuid4()
    if issubclass(id_class, str):
        return str(ObjectId())
    return None


def encode(entity: type, value: Any) -> Any:
    """*value* (a filter document, an id) encoded for the driver with the document's BSON encoders."""
    settings = entity.get_settings()  # type: ignore[attr-defined]
    return Encoder(custom_encoders=settings.bson_encoders).encode(value)


class InvalidIdError(InvalidPropertyError):
    """An id the document's id type does not accept (``"abc"`` for an ``ObjectId`` id)."""

    def __init__(self, entity: type, value: Any) -> None:
        super().__init__(
            f"{value!r} is not a valid id of {entity.__name__} (its id type is {id_type(entity)})",
            entity=entity.__name__,
            property="id",
            usage="id",
        )
