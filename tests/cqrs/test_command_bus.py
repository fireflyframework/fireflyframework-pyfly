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
"""Tests for DefaultCommandBus pipeline."""

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from pyfly.cqrs.authorization.exceptions import AuthorizationException
from pyfly.cqrs.authorization.service import AuthorizationService
from pyfly.cqrs.authorization.types import AuthorizationResult
from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.metrics import CqrsMetricsService
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.command.validation import CommandValidationService
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.exceptions import CommandProcessingException
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Command
from pyfly.cqrs.validation.exceptions import CqrsValidationException
from pyfly.cqrs.validation.types import ValidationResult

# -- Test messages ----------------------------------------------------------


@dataclass
class CreateOrderCommand(Command[str]):
    customer_id: str = ""
    amount: float = 0.0


@dataclass
class FailingCommand(Command[None]):
    pass


@dataclass
class InvalidCommand(Command[None]):
    value: str = ""

    async def validate(self) -> ValidationResult:
        if not self.value:
            return ValidationResult.failure("value", "value is required")
        return ValidationResult.success()


@dataclass
class UnauthorizedCommand(Command[None]):
    pass

    async def authorize(self) -> AuthorizationResult:
        return AuthorizationResult.failure("order", "Access denied")


# -- Test handlers ----------------------------------------------------------


class CreateOrderHandler(CommandHandler[CreateOrderCommand, str]):
    async def do_handle(self, command: CreateOrderCommand) -> str:
        return f"order-{command.customer_id}"


class ContextAwareOrderHandler(CommandHandler[CreateOrderCommand, str]):
    async def do_handle_with_context(self, command: CreateOrderCommand, context) -> str:
        return f"order-{command.customer_id}-by-{context.user_id}"


class FailingHandler(CommandHandler[FailingCommand, None]):
    async def do_handle(self, command: FailingCommand) -> None:
        raise ValueError("Handler exploded")


class InvalidCommandHandler(CommandHandler[InvalidCommand, None]):
    async def do_handle(self, command: InvalidCommand) -> None:
        return None


class UnauthorizedCommandHandler(CommandHandler[UnauthorizedCommand, None]):
    async def do_handle(self, command: UnauthorizedCommand) -> None:
        return None


# -- Tests ------------------------------------------------------------------


class TestDefaultCommandBus:
    @pytest.fixture(autouse=True)
    def _clear_correlation(self) -> None:
        CorrelationContext.clear()

    @pytest.fixture
    def registry(self) -> HandlerRegistry:
        return HandlerRegistry()

    @pytest.fixture
    def bus(self, registry: HandlerRegistry) -> DefaultCommandBus:
        return DefaultCommandBus(registry=registry)

    async def test_send_dispatches_to_correct_handler(self, bus: DefaultCommandBus, registry: HandlerRegistry) -> None:
        registry.register_command_handler(CreateOrderHandler())
        result = await bus.send(CreateOrderCommand(customer_id="cust-1"))
        assert result == "order-cust-1"

    async def test_send_with_context_passes_context(self, registry: HandlerRegistry) -> None:
        handler = ContextAwareOrderHandler()
        registry.register_command_handler(handler)
        bus = DefaultCommandBus(registry=registry)

        ctx = ExecutionContextBuilder().with_user_id("admin").build()
        result = await bus.send_with_context(
            CreateOrderCommand(customer_id="cust-2"),
            ctx,
        )
        assert result == "order-cust-2-by-admin"

    async def test_pipeline_validation_failure_raises(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(InvalidCommandHandler())
        validation = CommandValidationService()
        bus = DefaultCommandBus(registry=registry, validation=validation)

        with pytest.raises(CommandProcessingException) as exc_info:
            await bus.send(InvalidCommand(value=""))
        assert exc_info.value.cause is not None
        assert isinstance(exc_info.value.cause, CqrsValidationException)
        assert "value is required" in str(exc_info.value.cause)

    async def test_pipeline_validation_success_proceeds(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(InvalidCommandHandler())
        validation = CommandValidationService()
        bus = DefaultCommandBus(registry=registry, validation=validation)

        result = await bus.send(InvalidCommand(value="valid"))
        assert result is None

    async def test_pipeline_authorization_failure_raises(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(UnauthorizedCommandHandler())
        authorization = AuthorizationService(enabled=True)
        bus = DefaultCommandBus(registry=registry, authorization=authorization)

        with pytest.raises(CommandProcessingException) as exc_info:
            await bus.send(UnauthorizedCommand())
        assert exc_info.value.cause is not None
        assert isinstance(exc_info.value.cause, AuthorizationException)

    async def test_pipeline_authorization_disabled_allows(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(UnauthorizedCommandHandler())
        authorization = AuthorizationService(enabled=False)
        bus = DefaultCommandBus(registry=registry, authorization=authorization)

        result = await bus.send(UnauthorizedCommand())
        assert result is None

    async def test_handler_error_wraps_in_processing_exception(
        self, bus: DefaultCommandBus, registry: HandlerRegistry
    ) -> None:
        registry.register_command_handler(FailingHandler())

        with pytest.raises(CommandProcessingException) as exc_info:
            await bus.send(FailingCommand())
        assert exc_info.value.command_type is FailingCommand
        assert exc_info.value.cause is not None
        assert "Handler exploded" in str(exc_info.value.cause)

    async def test_correlation_id_set_from_command(self, bus: DefaultCommandBus, registry: HandlerRegistry) -> None:
        registry.register_command_handler(CreateOrderHandler())
        cmd = CreateOrderCommand(customer_id="cust-1")
        cmd.set_correlation_id("my-corr-id")
        await bus.send(cmd)

        assert cmd.get_correlation_id() == "my-corr-id"
        assert CorrelationContext.get_correlation_id() == "my-corr-id"

    async def test_correlation_id_auto_generated_when_missing(
        self, bus: DefaultCommandBus, registry: HandlerRegistry
    ) -> None:
        registry.register_command_handler(CreateOrderHandler())
        cmd = CreateOrderCommand(customer_id="cust-1")
        assert cmd.get_correlation_id() is None

        await bus.send(cmd)

        assert cmd.get_correlation_id() is not None
        assert len(cmd.get_correlation_id()) == 36  # UUID format

    async def test_metrics_record_success(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(CreateOrderHandler())
        metrics = CqrsMetricsService()

        with patch.object(metrics, "record_command_success") as mock_success:
            bus = DefaultCommandBus(registry=registry, metrics=metrics)
            await bus.send(CreateOrderCommand(customer_id="c1"))
            mock_success.assert_called_once()

            call_args = mock_success.call_args
            assert isinstance(call_args[0][0], CreateOrderCommand)
            assert isinstance(call_args[0][1], float)

    async def test_metrics_record_failure(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(FailingHandler())
        metrics = CqrsMetricsService()

        with patch.object(metrics, "record_command_failure") as mock_failure:
            bus = DefaultCommandBus(registry=registry, metrics=metrics)
            with pytest.raises(CommandProcessingException):
                await bus.send(FailingCommand())
            mock_failure.assert_called_once()

            call_args = mock_failure.call_args
            assert isinstance(call_args[0][0], FailingCommand)
            assert isinstance(call_args[0][1], Exception)
            assert isinstance(call_args[0][2], float)

    async def test_register_handler(self, bus: DefaultCommandBus) -> None:
        handler = CreateOrderHandler()
        bus.register_handler(handler)
        assert bus.has_handler(CreateOrderCommand) is True

    async def test_unregister_handler(self, bus: DefaultCommandBus) -> None:
        bus.register_handler(CreateOrderHandler())
        assert bus.has_handler(CreateOrderCommand) is True
        bus.unregister_handler(CreateOrderCommand)
        assert bus.has_handler(CreateOrderCommand) is False

    async def test_has_handler_false_initially(self, bus: DefaultCommandBus) -> None:
        assert bus.has_handler(CreateOrderCommand) is False

    async def test_full_pipeline_order(self, registry: HandlerRegistry) -> None:
        execution_log: list[str] = []

        original_validate = CommandValidationService.validate_command

        async def tracking_validate(self, command):
            execution_log.append("validate")
            return await original_validate(self, command)

        original_authorize = AuthorizationService.authorize_command

        async def tracking_authorize(self, command, context=None):
            execution_log.append("authorize")
            return await original_authorize(self, command, context)

        registry.register_command_handler(CreateOrderHandler())
        validation = CommandValidationService()
        authorization = AuthorizationService(enabled=True)
        bus = DefaultCommandBus(
            registry=registry,
            validation=validation,
            authorization=authorization,
        )

        with (
            patch.object(CommandValidationService, "validate_command", tracking_validate),
            patch.object(AuthorizationService, "authorize_command", tracking_authorize),
        ):
            await bus.send(CreateOrderCommand(customer_id="c1"))

        assert execution_log == ["validate", "authorize"]
