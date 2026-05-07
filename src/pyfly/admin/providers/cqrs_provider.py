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
"""CQRS data provider -- command/query handler listing and bus introspection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class CqrsProvider:
    """Provides CQRS handler information and bus pipeline details."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_handlers(self) -> dict[str, Any]:
        handlers: list[dict[str, Any]] = []
        pipeline: dict[str, Any] = {
            "command_bus": False,
            "query_bus": False,
            "validation": False,
            "authorization": False,
            "metrics": False,
            "event_publishing": False,
        }
        try:
            from pyfly.cqrs import HandlerRegistry

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, HandlerRegistry):
                    registry = reg.instance
                    for cmd_type in registry.get_registered_command_types():
                        cmd_handler = registry.find_command_handler(cmd_type)
                        handlers.append(
                            {
                                "message_type": f"{cmd_type.__module__}.{cmd_type.__qualname__}",
                                "message_name": cmd_type.__name__,
                                "handler_type": f"{type(cmd_handler).__module__}.{type(cmd_handler).__qualname__}",
                                "handler_name": type(cmd_handler).__name__,
                                "kind": "command",
                            }
                        )
                    for query_type in registry.get_registered_query_types():
                        query_handler = registry.find_query_handler(query_type)
                        handlers.append(
                            {
                                "message_type": f"{query_type.__module__}.{query_type.__qualname__}",
                                "message_name": query_type.__name__,
                                "handler_type": f"{type(query_handler).__module__}.{type(query_handler).__qualname__}",
                                "handler_name": type(query_handler).__name__,
                                "kind": "query",
                            }
                        )

            # Detect bus pipeline features
            pipeline.update(self._detect_pipeline())
        except ImportError:
            pass
        return {"handlers": handlers, "total": len(handlers), "pipeline": pipeline}

    def _detect_pipeline(self) -> dict[str, Any]:
        """Introspect registered CQRS buses for pipeline features."""
        result: dict[str, Any] = {}
        try:
            from pyfly.cqrs.command.bus import DefaultCommandBus
            from pyfly.cqrs.query.bus import DefaultQueryBus

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is None:
                    continue
                if isinstance(reg.instance, DefaultCommandBus):
                    bus = reg.instance
                    result["command_bus"] = True
                    result["validation"] = bus._validation is not None
                    result["authorization"] = bus._authorization is not None
                    result["metrics"] = bus._metrics is not None
                    result["event_publishing"] = bus._event_publisher is not None
                elif isinstance(reg.instance, DefaultQueryBus):
                    result["query_bus"] = True
        except ImportError:
            pass
        return result
