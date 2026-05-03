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
"""Domain event decorators for CQRS.

Mirrors Java's ``@PublishDomainEvent`` annotation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T", bound=type)


def publish_domain_event(
    *,
    destination: str | None = None,
    message_format: str = "json",
) -> Callable[..., Any]:
    """Mark a command handler to publish domain events after execution.

    The bus reads this metadata and publishes events returned by
    ``result.domain_events`` or ``command.domain_events``.

    Args:
        destination: Target topic/queue.  ``None`` uses the default.
        message_format: ``"json"`` or ``"avro"``.

    Usage::

        @publish_domain_event(destination="orders.events")
        @command_handler
        class CreateOrderHandler(CommandHandler[CreateOrderCmd, OrderId]):
            ...
    """

    def decorator(cls: T) -> T:
        cls.__pyfly_publish_event__ = True  # type: ignore[attr-defined]
        cls.__pyfly_event_destination__ = destination  # type: ignore[attr-defined]
        cls.__pyfly_event_format__ = message_format  # type: ignore[attr-defined]
        return cls

    return decorator
