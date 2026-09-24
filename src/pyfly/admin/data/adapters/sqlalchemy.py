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
"""Administration of existing SQLAlchemy models using per-operation sessions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import String, func, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer
from sqlalchemy.orm.exc import StaleDataError

from pyfly.admin.data.identifiers import EditTokens, decode_id, encode_id, json_value
from pyfly.admin.data.models import AdminField, AdminOperationContext, AdminPage, AdminQuery, AdminRecord, ModelAdmin
from pyfly.kernel.exceptions import (
    ConflictException,
    ForbiddenException,
    ResourceNotFoundException,
    ValidationException,
)

_READ_ONLY = {"created_at", "updated_at", "created_by", "updated_by", "version", "deleted_at"}
_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    Decimal: "decimal",
    bool: "boolean",
    datetime: "datetime",
    date: "date",
    UUID: "string",
    dict: "json",
    list: "json",
    bytes: "binary",
}


class SqlAlchemyAdminProvider:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, edit_token_key: str = "") -> None:
        self._factory = session_factory
        self._tokens = EditTokens(edit_token_key)

    @staticmethod
    def _columns(resource: ModelAdmin) -> dict[str, Any]:
        mapper: Any = inspect(resource.model)
        return {prop.key: prop.columns[0] for prop in mapper.column_attrs}

    async def schema(self, resource: ModelAdmin) -> tuple[AdminField, ...]:
        return self.describe(resource)

    def describe(self, resource: ModelAdmin) -> tuple[AdminField, ...]:
        columns = self._columns(resource)
        result = []
        for name in resource.fields:
            if name not in columns:
                raise ValueError(f"{resource.resource_id}: unknown field {name}")
            column = columns[name]
            adapter = resource.field_adapters.get(name)
            try:
                python_type = column.type.python_type
            except NotImplementedError as exc:
                if adapter is None:
                    raise ValueError(
                        f"{resource.resource_id}: unsupported field {name}; configure a field adapter"
                    ) from exc
                python_type = object
            choices = (
                tuple(item.value for item in python_type)
                if isinstance(python_type, type) and issubclass(python_type, Enum)
                else ()
            )
            kind = adapter.type if adapter else "enum" if choices else _TYPES.get(python_type)
            if kind is None:
                raise ValueError(f"Unsupported admin field: {name}")
            readonly = (
                name not in resource.editable_fields or column.primary_key or name in _READ_ONLY or kind == "binary"
            )
            if name in resource.editable_fields and readonly:
                raise ValueError(f"{name} is a generated or read-only field")
            result.append(
                AdminField(
                    name,
                    name.replace("_", " ").title(),
                    kind,
                    required=not column.nullable
                    and column.default is None
                    and column.server_default is None
                    and not readonly,
                    nullable=column.nullable,
                    read_only=readonly,
                    choices=choices,
                    relation=resource.relations.get(name),
                )
            )
        if any(not isinstance(columns[name].type, String) for name in resource.search_fields):
            raise ValueError("Search fields must be string columns")
        return tuple(result)

    def _predicates(self, resource: ModelAdmin, context: AdminOperationContext, id: str | None = None) -> list[Any]:
        columns = self._columns(resource)
        predicates: list[Any] = []
        if id is not None:
            keys = [name for name, col in columns.items() if col.primary_key]
            values = decode_id(id, [columns[name].type.python_type for name in keys])
            predicates.extend(getattr(resource.model, name) == value for name, value in zip(keys, values, strict=True))
        for name, value in resource.scope(context).items():
            if name not in columns:
                raise ValueError("Invalid administration scope field")
            predicates.append(getattr(resource.model, name) == value)
        if "deleted_at" in columns:
            predicates.append(getattr(resource.model, "deleted_at").is_(None))  # noqa: B009
        return predicates

    def _snapshot(self, resource: ModelAdmin, instance: Any) -> dict[str, Any]:
        return {name: getattr(instance, name) for name in self._columns(resource)}

    def _record(self, resource: ModelAdmin, instance: Any) -> AdminRecord:
        snapshot = self._snapshot(resource, instance)
        keys = [snapshot[name] for name, col in self._columns(resource).items() if col.primary_key]
        values = {
            name: json_value(resource.field_adapters[name].serialize(snapshot[name]))
            if name in resource.field_adapters and snapshot[name] is not None
            else json_value(snapshot[name])
            for name in resource.fields
        }
        return AdminRecord(encode_id(keys), values, self._tokens.issue(resource.resource_id, snapshot))

    async def list(self, resource: ModelAdmin, query: AdminQuery, context: AdminOperationContext) -> AdminPage:
        columns = self._columns(resource)
        predicates = self._predicates(resource, context)
        for name, value in query.filters.items():
            try:
                typed = (
                    resource.field_adapters[name].parse(value)
                    if name in resource.field_adapters
                    else TypeAdapter(columns[name].type.python_type).validate_python(value)
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise ValidationException("Invalid filter value") from exc
            predicates.append(getattr(resource.model, name) == typed)
        if query.search:
            if not resource.search_fields:
                raise ValidationException("Search is not configured")
            predicates.append(
                or_(
                    *(
                        getattr(resource.model, name).icontains(query.search, autoescape=True)
                        for name in resource.search_fields
                        if isinstance(columns[name].type, String)
                    )
                )
            )
        ordering = query.sort or resource.ordering
        names = [name.lstrip("-") for name in ordering]
        ordering = (*ordering, *(name for name, col in columns.items() if col.primary_key and name not in names))
        if any(name.lstrip("-") not in columns for name in ordering):
            raise ValidationException("Invalid ordering")
        statement: Any = select(resource.model).options(undefer("*")).where(*predicates)
        orders = [
            getattr(resource.model, name.lstrip("-")).desc()
            if name.startswith("-")
            else getattr(resource.model, name).asc()
            for name in ordering
        ]
        async with self._factory() as session:
            count = await session.scalar(select(func.count()).select_from(resource.model).where(*predicates))
            instances = (
                await session.scalars(
                    statement.order_by(*orders).offset((query.page - 1) * query.size).limit(query.size)
                )
            ).all()
            return AdminPage(
                [self._record(resource, instance) for instance in instances], int(count or 0), query.page, query.size
            )

    async def get(self, resource: ModelAdmin, id: str, context: AdminOperationContext) -> AdminRecord | None:
        async with self._factory() as session:
            statement: Any = (
                select(resource.model).options(undefer("*")).where(*self._predicates(resource, context, id))
            )
            instance = await session.scalar(statement)
            return self._record(resource, instance) if instance is not None else None

    async def _validate(self, resource: ModelAdmin, values: dict[str, Any], current: Any = None) -> dict[str, Any]:
        fields = await self.schema(resource)
        columns = self._columns(resource)
        clean: dict[str, Any] = {}
        errors = []
        for field in fields:
            if field.name not in values:
                if current is None and field.required:
                    errors.append({"loc": [field.name], "msg": "Required field"})
                continue
            if field.read_only:
                raise ValidationException("Read-only field")
            column = columns[field.name]
            try:
                if field.name in resource.field_adapters:
                    raw = values[field.name]
                    if raw is None and not column.nullable:
                        raise ValueError("Non-nullable field")
                    value = resource.field_adapters[field.name].parse(raw) if raw is not None else None
                else:
                    annotation = column.type.python_type
                    if column.nullable:
                        annotation = annotation | None
                    value = TypeAdapter(annotation).validate_python(values[field.name])
                if (
                    isinstance(column.type, String)
                    and column.type.length
                    and isinstance(value, str)
                    and len(value) > column.type.length
                ):
                    raise ValueError("Too long")
                clean[field.name] = value
            except (ValueError, TypeError):
                errors.append({"loc": [field.name], "msg": "Invalid value"})
        if errors:
            raise ValidationException("Invalid fields", context={"errors": errors})
        schema = resource.update_schema if current is not None else resource.create_schema
        if schema is not None:
            try:
                candidate = (
                    {name: getattr(current, name) for name in resource.editable_fields} if current is not None else {}
                )
                candidate.update(clean)
                validated = schema.model_validate(candidate).model_dump()
                clean = {name: value for name, value in validated.items() if name in resource.editable_fields}
            except ValidationError as exc:
                raise ValidationException(
                    "Invalid fields",
                    context={"errors": [{"loc": list(error["loc"]), "msg": "Invalid value"} for error in exc.errors()]},
                ) from exc
        return clean

    async def create(self, resource: ModelAdmin, values: dict[str, Any], context: AdminOperationContext) -> AdminRecord:
        clean = await self._validate(resource, values)
        scope = dict(resource.scope(context))
        if any(name in clean and clean[name] != value for name, value in scope.items()):
            raise ForbiddenException("Record is outside the permitted scope")
        clean.update(scope)
        try:
            async with self._factory() as session, session.begin():
                instance = resource.model(**clean)
                session.add(instance)
                await session.flush()
                await session.refresh(instance, attribute_names=list(self._columns(resource)))
                record = self._record(resource, instance)
            return record
        except (IntegrityError, StaleDataError) as exc:
            raise ConflictException("Record violates a database constraint") from exc

    async def _mutate(
        self, resource: ModelAdmin, id: str, values: dict[str, Any] | None, token: str, context: AdminOperationContext
    ) -> AdminRecord | None:
        operation = "delete" if values is None else "update"
        try:
            async with self._factory() as session:
                async with session.begin():
                    if session.get_bind().dialect.name == "sqlite":
                        await session.execute(text("BEGIN IMMEDIATE"))
                    statement: Any = (
                        select(resource.model)
                        .options(undefer("*"))
                        .where(*self._predicates(resource, context, id))
                        .with_for_update()
                    )
                    instance = await session.scalar(statement)
                    if instance is None:
                        raise ResourceNotFoundException("Record not found")
                    snapshot = self._snapshot(resource, instance)
                    if not resource.has_permission(
                        operation, context, {name: snapshot[name] for name in resource.fields}
                    ):
                        raise ForbiddenException("Operation is not permitted for this record")
                    self._tokens.check(resource.resource_id, snapshot, token)
                    if values is None:
                        if "deleted_at" in snapshot:
                            instance.deleted_at = datetime.now(UTC)
                        else:
                            await session.delete(instance)
                        record = None
                    else:
                        clean = await self._validate(resource, values, instance)
                        if any(
                            name in clean and clean[name] != value for name, value in resource.scope(context).items()
                        ):
                            raise ForbiddenException("Record is outside the permitted scope")
                        for name, value in clean.items():
                            setattr(instance, name, value)
                        await session.flush()
                        await session.refresh(instance, attribute_names=list(self._columns(resource)))
                        record = self._record(resource, instance)
                return record
        except (IntegrityError, StaleDataError, OperationalError) as exc:
            raise ConflictException("Concurrent change or database constraint; reload the record") from exc

    async def update(
        self, resource: ModelAdmin, id: str, values: dict[str, Any], edit_token: str, context: AdminOperationContext
    ) -> AdminRecord:
        record = await self._mutate(resource, id, values, edit_token, context)
        assert record is not None
        return record

    async def delete(self, resource: ModelAdmin, id: str, edit_token: str, context: AdminOperationContext) -> None:
        await self._mutate(resource, id, None, edit_token, context)
