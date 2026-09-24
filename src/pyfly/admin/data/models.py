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
"""Administration metadata; the model itself remains the application's ORM model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from pyfly.security.context import SecurityContext

if TYPE_CHECKING:
    from pyfly.admin.data.ports import AdminDataProvider


@dataclass(frozen=True)
class AdminOperationContext:
    security: SecurityContext
    correlation_id: str = ""


@dataclass(frozen=True)
class AdminField:
    name: str
    label: str
    type: str
    required: bool = False
    nullable: bool = False
    read_only: bool = True
    choices: tuple[Any, ...] = ()
    relation: str | None = None


@dataclass(frozen=True)
class AdminRecord:
    id: str
    values: dict[str, Any]
    edit_token: str = ""


@dataclass(frozen=True)
class AdminPage:
    items: list[AdminRecord]
    total: int
    page: int
    size: int


@dataclass(frozen=True)
class AdminQuery:
    page: int = 1
    size: int = 25
    search: str = ""
    sort: tuple[str, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdminFieldAdapter:
    type: str
    serialize: Callable[[Any], Any]
    parse: Callable[[Any], Any]


@dataclass
class ModelAdmin:
    resource_id: str
    model: type
    datasource: str = "primary"
    label: str = ""
    fields: tuple[str, ...] = ()
    editable_fields: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    filter_fields: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()
    operations: tuple[str, ...] = ("list", "read")
    create_schema: type[BaseModel] | None = None
    update_schema: type[BaseModel] | None = None
    relations: Mapping[str, str] = field(default_factory=dict)
    provider: AdminDataProvider | None = None
    field_adapters: Mapping[str, AdminFieldAdapter] = field(default_factory=dict)

    def has_permission(
        self,
        operation: str,
        context: AdminOperationContext,
        record: Mapping[str, Any] | None = None,
    ) -> bool:
        return operation in self.operations

    def scope(self, context: AdminOperationContext) -> Mapping[str, Any]:
        """Server-owned equality restrictions, applied before queries and mutations."""
        return {}
