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
"""Administration of initialized Beanie documents, with atomic conditional writes.

Ids are parsed with the document's own id type (an ``ObjectId``, a string, a ``UUID``...), and a new document
gets its id on the client when its type allows it, as the repository does (C090). Each write costs one round
trip: a create builds its record from the document it inserted (through the collection's own BSON codec, so
the record and its edit token are what a read would give), and an update or a delete checks the document it
read and writes it in one conditional ``findAndModify`` (``find_one_and_replace``/``find_one_and_delete``),
which fails when the document changed since.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast, get_args
from uuid import uuid4

import bson
from beanie import Document
from beanie.odm.utils.dump import get_dict
from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from pyfly.admin.data.identifiers import EditTokens, decode_id, json_value
from pyfly.admin.data.models import AdminField, AdminOperationContext, AdminPage, AdminQuery, AdminRecord, ModelAdmin
from pyfly.data.auditing import active_auditing_handler
from pyfly.data.document.mongodb.properties import encode, id_type, new_id
from pyfly.kernel.exceptions import (
    ConflictException,
    ForbiddenException,
    ResourceNotFoundException,
    ValidationException,
)

_READ_ONLY = {"id", "revision_id", "created_at", "updated_at", "created_by", "updated_by", "deleted_at"}


class BeanieAdminProvider:
    def __init__(self, *, edit_token_key: str = "") -> None:
        self._tokens = EditTokens(edit_token_key)

    @staticmethod
    def _model(resource: ModelAdmin) -> type[Document]:
        return cast(type[Document], resource.model)

    @staticmethod
    def _alias(resource: ModelAdmin, name: str) -> str:
        field = BeanieAdminProvider._model(resource).model_fields.get(name)
        if field is None:
            raise ValueError(f"Unknown document field: {name}")
        return str(field.alias or name)

    async def schema(self, resource: ModelAdmin) -> tuple[AdminField, ...]:
        return self.describe(resource)

    def describe(self, resource: ModelAdmin) -> tuple[AdminField, ...]:
        model = self._model(resource)
        properties = model.model_json_schema()["properties"]
        result = []
        for name in resource.fields:
            field = model.model_fields[name]
            description = properties[self._alias(resource, name)]
            variants = description.get("anyOf", [description])
            nullable = any(v.get("type") == "null" for v in variants)
            value = next((v for v in variants if v.get("type") != "null"), description)
            kind = value.get("format", value.get("type", "json"))
            kind = {"date-time": "datetime", "object": "json", "array": "json"}.get(kind, kind)
            if field.annotation is Decimal or Decimal in get_args(field.annotation):
                kind = "decimal"
            readonly = name not in resource.editable_fields or name in _READ_ONLY
            if name in resource.editable_fields and readonly:
                raise ValueError(f"{name} is a read-only document field")
            if "$ref" in value:
                reference = value["$ref"].split("/")[-1]
                value = model.model_json_schema().get("$defs", {}).get(reference, value)
                kind = "enum" if "enum" in value else "json"
            result.append(
                AdminField(
                    name,
                    name.replace("_", " ").title(),
                    resource.field_adapters[name].type if name in resource.field_adapters else kind,
                    required=field.is_required() and not readonly,
                    nullable=nullable,
                    read_only=readonly,
                    choices=tuple(value.get("enum", ())),
                    relation=resource.relations.get(name),
                )
            )
        return tuple(result)

    def _filter(self, resource: ModelAdmin, context: AdminOperationContext, id: str | None = None) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if id is not None:
            parts.append({"_id": self._id(resource, id)})
        for name, value in resource.scope(context).items():
            parts.append({self._alias(resource, name): {"$eq": value}})
        if "deleted_at" in BeanieAdminProvider._model(resource).model_fields:
            parts.append({self._alias(resource, "deleted_at"): None})
        return {"$and": parts} if parts else {}

    def _id(self, resource: ModelAdmin, value: str) -> Any:
        """*value* (a record id) as the document's id type; an id the type cannot hold is a 400."""
        try:
            (parsed,) = decode_id(value, [id_type(self._model(resource))])
        except ValidationException as exc:
            raise ValidationException("Invalid document identifier") from exc
        if parsed is None:
            raise ValidationException("Invalid document identifier")
        return encode(self._model(resource), parsed)  # a UUID as BSON binary, whatever the client's settings

    def _record(self, resource: ModelAdmin, raw: dict[str, Any]) -> AdminRecord:
        document = self._model(resource).model_validate(raw)
        values = document.model_dump(mode="json", include=set(resource.fields) - set(resource.field_adapters))
        for name, adapter in resource.field_adapters.items():
            value = getattr(document, name)
            values[name] = adapter.serialize(value) if value is not None else None
        values = {name: json_value(value) for name, value in values.items()}
        return AdminRecord(_record_id(raw["_id"]), values, self._tokens.issue(resource.resource_id, raw))

    async def list(self, resource: ModelAdmin, query: AdminQuery, context: AdminOperationContext) -> AdminPage:
        from pydantic import TypeAdapter

        criteria = [self._filter(resource, context)]
        for name, value in query.filters.items():
            try:
                validated = TypeAdapter(
                    BeanieAdminProvider._model(resource).model_fields[name].annotation
                ).validate_python(value)
            except (KeyError, ValueError) as exc:
                raise ValidationException("Invalid filter") from exc
            criteria.append({self._alias(resource, name): {"$eq": validated}})
        if query.search:
            if not resource.search_fields:
                raise ValidationException("Search is not configured")
            criteria.append(
                {
                    "$or": [
                        {self._alias(resource, name): {"$regex": re.escape(query.search), "$options": "i"}}
                        for name in resource.search_fields
                    ]
                }
            )
        where = {"$and": criteria}
        orders = [
            (self._alias(resource, name.lstrip("-")), -1 if name.startswith("-") else 1)
            for name in (query.sort or resource.ordering)
        ]
        if not any(name == "_id" for name, _ in orders):
            orders.append(("_id", 1))
        collection = self._model(resource).get_pymongo_collection()
        total = await collection.count_documents(where)
        rows = await collection.find(where).sort(orders).skip((query.page - 1) * query.size).limit(query.size).to_list()
        return AdminPage([self._record(resource, row) for row in rows], total, query.page, query.size)

    async def get(self, resource: ModelAdmin, id: str, context: AdminOperationContext) -> AdminRecord | None:
        raw = await self._model(resource).get_pymongo_collection().find_one(self._filter(resource, context, id))
        return self._record(resource, raw) if raw else None

    def _validate(
        self,
        resource: ModelAdmin,
        values: dict[str, Any],
        context: AdminOperationContext,
        snapshot: dict[str, Any] | None = None,
    ) -> Document:
        scope = dict(resource.scope(context))
        if any(name in values and values[name] != value for name, value in scope.items()):
            raise ForbiddenException("Record is outside the permitted scope")
        candidate = dict(snapshot or {})
        supplied = {
            name: resource.field_adapters[name].parse(value)
            if name in resource.field_adapters and value is not None
            else value
            for name, value in values.items()
        }
        schema = resource.update_schema if snapshot is not None else resource.create_schema
        try:
            if schema is not None:
                previous = (
                    {name: candidate.get(self._alias(resource, name)) for name in resource.editable_fields}
                    if snapshot
                    else {}
                )
                previous.update(supplied)
                supplied = schema.model_validate(previous).model_dump()
            for name, value in {**supplied, **scope}.items():
                candidate[self._alias(resource, name)] = value
            model = self._model(resource)
            now = _now()
            for name, value in (("updated_at", now), ("updated_by", context.security.user_id)):
                if name in model.model_fields:
                    candidate[self._alias(resource, name)] = value
            if snapshot is None:
                for name, value in (("created_at", now), ("created_by", context.security.user_id)):
                    if name in model.model_fields:
                        candidate[self._alias(resource, name)] = value
            if model.get_settings().use_revision:
                candidate["revision_id"] = uuid4()
            if snapshot is None and candidate.get("_id") is None and candidate.get("id") is None:
                generated = new_id(model)
                if generated is not None:
                    candidate["_id"] = generated
            return cast(Document, model.model_validate(candidate))
        except ValidationError as exc:
            raise ValidationException(
                "Invalid fields",
                context={"errors": [{"loc": list(error["loc"]), "msg": "Invalid value"} for error in exc.errors()]},
            ) from exc

    async def create(self, resource: ModelAdmin, values: dict[str, Any], context: AdminOperationContext) -> AdminRecord:
        document = self._validate(resource, values, context)
        collection = self._model(resource).get_pymongo_collection()
        inserted = get_dict(document, to_db=True)
        try:
            await collection.insert_one(inserted)  # sets inserted["_id"] when the server assigns it
        except DuplicateKeyError as exc:
            raise ConflictException("Record violates a uniqueness constraint") from exc
        return self._record(resource, _as_stored(collection, inserted))

    async def _mutate(
        self, resource: ModelAdmin, id: str, values: dict[str, Any] | None, token: str, context: AdminOperationContext
    ) -> AdminRecord | None:
        collection = self._model(resource).get_pymongo_collection()
        raw = await collection.find_one(self._filter(resource, context, id))
        if raw is None:
            raise ResourceNotFoundException("Record not found")
        operation = "delete" if values is None else "update"
        if not resource.has_permission(operation, context, self._record(resource, raw).values):
            raise ForbiddenException("Operation is not permitted for this record")
        self._tokens.check(resource.resource_id, raw, token)
        criteria = {"$and": [self._filter(resource, context, id), {"$expr": {"$eq": ["$$ROOT", {"$literal": raw}]}}]}
        record: AdminRecord | None = None
        try:
            if values is None and "deleted_at" not in BeanieAdminProvider._model(resource).model_fields:
                matched = await collection.find_one_and_delete(criteria, projection={"_id": 1}) is not None
            else:
                document = self._validate(resource, values or {}, context, raw)
                if values is None:
                    document.deleted_at = _now()
                replacement = {**raw, **get_dict(document, to_db=True)}
                saved = await collection.find_one_and_replace(
                    criteria, replacement, return_document=ReturnDocument.AFTER
                )
                matched = saved is not None
                if saved is not None and values is not None:
                    record = self._record(resource, saved)
        except DuplicateKeyError as exc:
            raise ConflictException("Record violates a uniqueness constraint") from exc
        if not matched:
            raise ConflictException("This record has changed; reload before saving")
        return record

    async def update(
        self, resource: ModelAdmin, id: str, values: dict[str, Any], edit_token: str, context: AdminOperationContext
    ) -> AdminRecord:
        result = await self._mutate(resource, id, values, edit_token, context)
        assert result is not None
        return result

    async def delete(self, resource: ModelAdmin, id: str, edit_token: str, context: AdminOperationContext) -> None:
        await self._mutate(resource, id, None, edit_token, context)


def _now() -> datetime:
    """The auditing clock's time (UTC now when auditing is off), cut to the millisecond BSON stores."""
    handler = active_auditing_handler()
    moment = handler.now() if handler is not None else datetime.now(UTC)
    return moment.replace(microsecond=moment.microsecond - moment.microsecond % 1000)


def _as_stored(collection: Any, document: dict[str, Any]) -> dict[str, Any]:
    """*document* as a read of it gives it back: through the collection's BSON codec (its time zone, UUID and
    type settings), so the record and its edit token match those of a later read."""
    options = collection.codec_options
    return cast(dict[str, Any], bson.decode(bson.encode(document, codec_options=options), codec_options=options))


def _record_id(stored: Any) -> str:
    """The record id of a stored ``_id``: a UUID read by a client without a UUID representation is still one."""
    if isinstance(stored, bson.Binary) and stored.subtype in (bson.binary.UUID_SUBTYPE, bson.binary.OLD_UUID_SUBTYPE):
        return str(stored.as_uuid(stored.subtype))
    return str(stored)
