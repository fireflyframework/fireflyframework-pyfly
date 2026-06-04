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
"""Mappings actuator endpoint — Spring Boot ``/actuator/mappings`` parity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class MappingsEndpoint:
    """Exposes HTTP handler mappings at ``/actuator/mappings`` (contexts envelope)."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "mappings"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        mappings: list[dict[str, Any]] = []
        for cls, _reg in self._context.container._registrations.items():
            if getattr(cls, "__pyfly_stereotype__", "") not in ("rest_controller", "controller"):
                continue
            base_path = getattr(cls, "__pyfly_request_mapping__", "")
            for attr_name in dir(cls):
                method_obj = getattr(cls, attr_name, None)
                if method_obj is None:
                    continue
                mapping = getattr(method_obj, "__pyfly_mapping__", None)
                if mapping is None:
                    continue
                full_path = base_path + mapping["path"]
                http_method = mapping["method"]
                mappings.append(
                    {
                        "handler": f"{cls.__name__}#{attr_name}",
                        "predicate": f"{{{http_method} {full_path}}}",
                        "details": {
                            "handlerMethod": {
                                "className": f"{cls.__module__}.{cls.__qualname__}",
                                "name": attr_name,
                            },
                            "requestMappingConditions": {
                                "methods": [http_method],
                                "patterns": [full_path],
                                "produces": [],
                                "consumes": [],
                            },
                        },
                    }
                )
        mappings.sort(key=lambda m: m["predicate"])

        return {
            "contexts": {
                "application": {
                    "mappings": {"dispatcherServlets": {"dispatcherServlet": mappings}},
                }
            }
        }
