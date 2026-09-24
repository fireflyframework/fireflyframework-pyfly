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
"""Explicit allowlist of models available to data administration."""

import re

from pyfly.admin.data.models import ModelAdmin
from pyfly.kernel.exceptions import ResourceNotFoundException


class AdminResourceRegistry:
    def __init__(self) -> None:
        self._resources: dict[str, ModelAdmin] = {}

    def register(self, registration: ModelAdmin) -> None:
        key = registration.resource_id
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}", key):
            raise ValueError("Invalid admin resource identifier")
        if key in self._resources:
            raise ValueError(f"Duplicate admin resource: {key}")
        if not registration.fields or len(registration.fields) != len(set(registration.fields)):
            raise ValueError(f"{key}: explicitly select unique visible fields")
        if not set(registration.editable_fields) <= set(registration.fields):
            raise ValueError(f"{key}: editable fields must be visible")
        for names in (
            registration.search_fields,
            registration.filter_fields,
            tuple(registration.relations),
            tuple(registration.field_adapters),
        ):
            if not set(names) <= set(registration.fields):
                raise ValueError(f"{key}: query/relation fields must be visible")
        if not set(registration.operations) <= {"list", "read", "create", "update", "delete"}:
            raise ValueError(f"{key}: invalid operations")
        if any(name.lstrip("-") not in registration.fields for name in registration.ordering):
            raise ValueError(f"{key}: ordering fields must be visible")
        if any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) for name in registration.fields):
            raise ValueError(f"{key}: invalid field name")
        self._resources[key] = registration

    def get(self, resource_id: str) -> ModelAdmin:
        try:
            return self._resources[resource_id]
        except KeyError:
            raise ResourceNotFoundException("Resource not found") from None

    def resources(self) -> tuple[ModelAdmin, ...]:
        return tuple(self._resources.values())
