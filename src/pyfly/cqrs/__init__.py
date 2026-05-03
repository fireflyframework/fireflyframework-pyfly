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
"""PyFly CQRS — Command/Query Responsibility Segregation.

Full-featured CQRS implementation ported from fireflyframework-cqrs (Java),
built around a bus-based architecture with validation, authorization,
caching, distributed tracing, and domain event publishing.

Quick start::

    from pyfly.cqrs import (
        Command, CommandHandler, CommandBus, DefaultCommandBus,
        Query, QueryHandler, QueryBus, DefaultQueryBus,
        HandlerRegistry, command_handler, query_handler,
    )

    @dataclass(frozen=True)
    class CreateOrder(Command[str]):
        customer: str

    @command_handler
    class CreateOrderHandler(CommandHandler[CreateOrder, str]):
        async def do_handle(self, cmd: CreateOrder) -> str:
            return f"order-{cmd.customer}"

    registry = HandlerRegistry()
    registry.register_command_handler(CreateOrderHandler())
    bus = DefaultCommandBus(registry=registry)
    result = await bus.send(CreateOrder(customer="alice"))
"""

# ── base types ────────────────────────────────────────────────
# ── authorization ─────────────────────────────────────────────
from pyfly.cqrs.authorization.exceptions import AuthorizationException
from pyfly.cqrs.authorization.types import AuthorizationError, AuthorizationResult, AuthorizationSeverity

# ── buses ─────────────────────────────────────────────────────
from pyfly.cqrs.command.bus import CommandBus, DefaultCommandBus

# ── handlers ──────────────────────────────────────────────────
from pyfly.cqrs.command.handler import CommandHandler, ContextAwareCommandHandler

# ── registry ──────────────────────────────────────────────────
from pyfly.cqrs.command.registry import HandlerRegistry

# ── context ───────────────────────────────────────────────────
from pyfly.cqrs.context.execution_context import DefaultExecutionContext, ExecutionContext, ExecutionContextBuilder

# ── decorators ────────────────────────────────────────────────
from pyfly.cqrs.decorators import command_handler, query_handler

# ── exceptions ────────────────────────────────────────────────
from pyfly.cqrs.exceptions import (
    CommandHandlerNotFoundException,
    CommandProcessingException,
    CqrsConfigurationException,
    CqrsException,
    QueryHandlerNotFoundException,
    QueryProcessingException,
)
from pyfly.cqrs.query.bus import DefaultQueryBus, QueryBus
from pyfly.cqrs.query.handler import ContextAwareQueryHandler, QueryHandler

# ── tracing ───────────────────────────────────────────────────
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Command, Query

# ── validation ────────────────────────────────────────────────
from pyfly.cqrs.validation.exceptions import CqrsValidationException
from pyfly.cqrs.validation.types import ValidationError, ValidationResult, ValidationSeverity

__all__ = [
    # base types
    "Command",
    "Query",
    # handlers
    "CommandHandler",
    "ContextAwareCommandHandler",
    "QueryHandler",
    "ContextAwareQueryHandler",
    # buses
    "CommandBus",
    "DefaultCommandBus",
    "QueryBus",
    "DefaultQueryBus",
    # registry
    "HandlerRegistry",
    # decorators
    "command_handler",
    "query_handler",
    # authorization
    "AuthorizationError",
    "AuthorizationException",
    "AuthorizationResult",
    "AuthorizationSeverity",
    # validation
    "CqrsValidationException",
    "ValidationError",
    "ValidationResult",
    "ValidationSeverity",
    # context
    "DefaultExecutionContext",
    "ExecutionContext",
    "ExecutionContextBuilder",
    # tracing
    "CorrelationContext",
    # exceptions
    "CommandHandlerNotFoundException",
    "CommandProcessingException",
    "CqrsConfigurationException",
    "CqrsException",
    "QueryHandlerNotFoundException",
    "QueryProcessingException",
]
