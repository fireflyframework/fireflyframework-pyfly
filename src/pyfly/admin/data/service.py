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
"""Authorized administration use cases; providers own persistence."""

from __future__ import annotations

import logging
from typing import Any

from pyfly.admin.data.config import AdminDataProperties
from pyfly.admin.data.models import AdminField, AdminOperationContext, AdminPage, AdminQuery, AdminRecord, ModelAdmin
from pyfly.admin.data.ports import AdminDataProvider
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.kernel.exceptions import (
    ForbiddenException,
    ResourceNotFoundException,
    UnauthorizedException,
    ValidationException,
)

_logger = logging.getLogger("pyfly.admin.data.audit")


class AdminDataService:
    def __init__(self, registry: AdminResourceRegistry, properties: AdminDataProperties | None = None) -> None:
        self.registry = registry
        self.properties = properties or AdminDataProperties()

    def authorize(self, resource_id: str, operation: str, context: AdminOperationContext) -> ModelAdmin:
        if not context.security.is_authenticated:
            raise UnauthorizedException("Authentication required")
        if not context.security.has_any_role(self.properties.allowed_roles):
            raise ForbiddenException("Data administration is forbidden")
        resource = self.registry.get(resource_id)
        if (
            operation not in self.properties.operations
            or operation not in resource.operations
            or not resource.has_permission(operation, context)
        ):
            raise ForbiddenException("Operation is not permitted")
        return resource

    @staticmethod
    def _provider(resource: ModelAdmin) -> AdminDataProvider:
        if resource.provider is None:
            raise ValueError(f"No data provider configured for {resource.resource_id}")
        return resource.provider

    async def schema(self, resource_id: str, context: AdminOperationContext) -> tuple[AdminField, ...]:
        resource = self.authorize(resource_id, "read", context)
        return await self._provider(resource).schema(resource)

    async def list(self, resource_id: str, query: AdminQuery, context: AdminOperationContext) -> AdminPage:
        resource = self.authorize(resource_id, "list", context)
        if not 1 <= query.size <= self.properties.max_page_size or query.page < 1 or query.page > 1000000:
            raise ValidationException("Invalid page or size")
        if len(query.search) > 256 or not set(query.filters) <= set(resource.filter_fields):
            raise ValidationException("Invalid search or filter")
        if any(name.lstrip("-") not in resource.fields for name in query.sort):
            raise ValidationException("Invalid ordering")
        return await self._provider(resource).list(resource, query, context)

    async def get(self, resource_id: str, id: str, context: AdminOperationContext) -> AdminRecord:
        resource = self.authorize(resource_id, "read", context)
        record = await self._provider(resource).get(resource, id, context)
        if record is None or not resource.has_permission("read", context, record.values):
            raise ResourceNotFoundException("Record not found")
        return record

    async def _values(self, resource: ModelAdmin, values: dict[str, Any], context: AdminOperationContext) -> None:
        if not isinstance(values, dict) or not set(values) <= set(resource.editable_fields):
            raise ValidationException("Unknown or read-only fields")
        fields = await self._provider(resource).schema(resource)
        if any(f.read_only and f.name in values for f in fields):
            raise ValidationException("Read-only fields cannot be modified")
        for name, target in resource.relations.items():
            if name in values and values[name] is not None:
                await self.get(target, str(values[name]), context)

    async def create(self, resource_id: str, values: dict[str, Any], context: AdminOperationContext) -> AdminRecord:
        resource = self.authorize(resource_id, "create", context)
        await self._values(resource, values, context)
        record = await self._provider(resource).create(resource, values, context)
        self._audit("create", resource, record.id, values, context)
        return record

    async def update(
        self, resource_id: str, id: str, values: dict[str, Any], edit_token: str, context: AdminOperationContext
    ) -> AdminRecord:
        resource = self.authorize(resource_id, "update", context)
        await self._values(resource, values, context)
        record = await self._provider(resource).update(resource, id, values, edit_token, context)
        self._audit("update", resource, id, values, context)
        return record

    async def delete(self, resource_id: str, id: str, edit_token: str, context: AdminOperationContext) -> None:
        resource = self.authorize(resource_id, "delete", context)
        await self._provider(resource).delete(resource, id, edit_token, context)
        self._audit("delete", resource, id, {}, context)

    @staticmethod
    def _audit(
        operation: str, resource: ModelAdmin, id: str, values: dict[str, Any], context: AdminOperationContext
    ) -> None:
        _logger.info(
            "admin_mutation actor=%s resource=%s id=%s operation=%s fields=%s correlation_id=%s result=success",
            context.security.user_id,
            resource.resource_id,
            id,
            operation,
            sorted(values),
            context.correlation_id,
        )
