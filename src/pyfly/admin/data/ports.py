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
"""Provider extension for ORM or application-service-backed administration."""

from typing import Any, Protocol

from pyfly.admin.data.models import AdminField, AdminOperationContext, AdminPage, AdminQuery, AdminRecord, ModelAdmin


class AdminDataProvider(Protocol):
    async def schema(self, resource: ModelAdmin) -> tuple[AdminField, ...]: ...
    async def list(self, resource: ModelAdmin, query: AdminQuery, context: AdminOperationContext) -> AdminPage: ...
    async def get(self, resource: ModelAdmin, id: str, context: AdminOperationContext) -> AdminRecord | None: ...
    async def create(
        self, resource: ModelAdmin, values: dict[str, Any], context: AdminOperationContext
    ) -> AdminRecord: ...
    async def update(
        self, resource: ModelAdmin, id: str, values: dict[str, Any], edit_token: str, context: AdminOperationContext
    ) -> AdminRecord: ...
    async def delete(self, resource: ModelAdmin, id: str, edit_token: str, context: AdminOperationContext) -> None: ...
