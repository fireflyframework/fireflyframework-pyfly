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
"""Beans actuator endpoint — Spring Boot ``/actuator/beans`` parity."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class BeansEndpoint:
    """Exposes the DI bean registry at ``/actuator/beans`` (contexts envelope)."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "beans"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        registrations = self._context.container._registrations
        type_to_name = {cls: (reg.name or cls.__name__) for cls, reg in registrations.items()}

        beans: dict[str, Any] = {}
        for cls, reg in registrations.items():
            bean_name = reg.name or cls.__name__
            beans[bean_name] = {
                "aliases": [],
                "scope": reg.scope.name.lower(),
                "type": f"{cls.__module__}.{cls.__qualname__}",
                "resource": getattr(cls, "__module__", ""),
                "dependencies": self._dependencies(cls, type_to_name),
                # pyfly extension — handy in the admin/UI, ignored by Spring tooling.
                "stereotype": getattr(cls, "__pyfly_stereotype__", "none"),
            }

        return {"contexts": {"application": {"beans": beans}}}

    @staticmethod
    def _dependencies(cls: type, type_to_name: dict[type, str]) -> list[str]:
        """Resolve constructor dependency bean names from ``__init__`` type hints."""
        deps: list[str] = []
        try:
            signature = inspect.signature(cls)
        except (ValueError, TypeError):
            return deps
        for name, param in signature.parameters.items():
            if name == "self":
                continue
            annotation = param.annotation
            dep_name = type_to_name.get(annotation)
            if dep_name:
                deps.append(dep_name)
            elif annotation is not inspect.Parameter.empty:
                deps.append(getattr(annotation, "__name__", str(annotation)))
        return deps
