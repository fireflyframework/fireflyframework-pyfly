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
"""Bind admin registrations to existing, application-owned datasource factories."""

from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import Any

from pyfly.admin.data.config import AdminDataProperties
from pyfly.admin.data.models import ModelAdmin
from pyfly.admin.data.registry import AdminResourceRegistry
from pyfly.admin.data.service import AdminDataService
from pyfly.container.exceptions import NoSuchBeanError


def build_data_routes(context: Any, base: str, hooks: list[Callable[[], None]]) -> list[Any]:
    properties = context.config.bind(AdminDataProperties)
    if not properties.enabled:
        return []
    from pyfly.admin.data.adapters.starlette import DataAdminRoutes

    routes = DataAdminRoutes(properties)

    def initialize() -> None:
        try:
            registrations = list(context.get_bean(AdminResourceRegistry).resources())
        except NoSuchBeanError:
            registrations = []
        for registration in context.get_beans_of_type(ModelAdmin):
            if not any(item is registration for item in registrations):
                registrations.append(registration)
        registry = AdminResourceRegistry()
        for registration in registrations:
            registry.register(copy(registration))
        for resource in registry.resources():
            if (
                set(resource.operations) & set(properties.operations) & {"create", "update", "delete"}
                and len(properties.edit_token_key) < 32
            ):
                raise ValueError("Data admin writes require pyfly.admin.data.edit-token-key (at least 32 characters)")
            if resource.provider is None:
                if resource.datasource == "document":
                    from pyfly.admin.data.adapters.beanie import BeanieAdminProvider

                    resource.provider = BeanieAdminProvider(edit_token_key=properties.edit_token_key)
                else:
                    from pyfly.admin.data.adapters.sqlalchemy import SqlAlchemyAdminProvider
                    from pyfly.data.relational.named_datasources import NamedDataSources

                    factory = (
                        context.get_bean_by_name("async_session_factory")
                        if resource.datasource == "primary"
                        else context.get_bean(NamedDataSources).get(resource.datasource)
                    )
                    resource.provider = SqlAlchemyAdminProvider(factory, edit_token_key=properties.edit_token_key)
            describe = getattr(resource.provider, "describe", None)
            if describe is not None:
                describe(resource)
            for target in resource.relations.values():
                registry.get(target)
        routes.service = AdminDataService(registry, properties)

    if context._started:
        initialize()
    hooks.append(initialize)
    return routes.build(base)
