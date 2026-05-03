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
"""Tests for the enhanced CommandHandler with lifecycle hooks."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cqrs.command.handler import CommandHandler, ContextAwareCommandHandler
from pyfly.cqrs.context.execution_context import DefaultExecutionContext
from pyfly.cqrs.types import Command

# ── test command types ────────────────────────────────────────


@dataclass(frozen=True)
class CreateItemCommand(Command[str]):
    name: str = "widget"


@dataclass(frozen=True)
class DeleteItemCommand(Command[None]):
    item_id: str = "item-1"


# ── handler that tracks lifecycle hook call order ─────────────


class TrackingCommandHandler(CommandHandler[CreateItemCommand, str]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def pre_process(self, command: CreateItemCommand) -> None:
        self.calls.append("pre_process")

    async def do_handle(self, command: CreateItemCommand) -> str:
        self.calls.append("do_handle")
        return f"created-{command.name}"

    async def post_process(self, command: CreateItemCommand, result: str) -> None:
        self.calls.append("post_process")

    async def on_success(self, command: CreateItemCommand, result: str) -> None:
        self.calls.append("on_success")

    async def on_error(self, command: CreateItemCommand, error: Exception) -> None:
        self.calls.append("on_error")


# ── handler that raises in do_handle ──────────────────────────


class FailingCommandHandler(CommandHandler[CreateItemCommand, str]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def pre_process(self, command: CreateItemCommand) -> None:
        self.calls.append("pre_process")

    async def do_handle(self, command: CreateItemCommand) -> str:
        self.calls.append("do_handle")
        raise ValueError("boom")

    async def post_process(self, command: CreateItemCommand, result: str) -> None:
        self.calls.append("post_process")

    async def on_success(self, command: CreateItemCommand, result: str) -> None:
        self.calls.append("on_success")

    async def on_error(self, command: CreateItemCommand, error: Exception) -> None:
        self.calls.append("on_error")


# ── handler that transforms errors via map_error ──────────────


class MappingErrorCommandHandler(CommandHandler[CreateItemCommand, str]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def do_handle(self, command: CreateItemCommand) -> str:
        raise ValueError("original")

    async def on_error(self, command: CreateItemCommand, error: Exception) -> None:
        self.calls.append("on_error")

    def map_error(self, command: CreateItemCommand, error: Exception) -> Exception:
        self.calls.append("map_error")
        return RuntimeError(f"mapped: {error}")


# ── context-aware handler ─────────────────────────────────────


class ContextAwareCreateHandler(ContextAwareCommandHandler[CreateItemCommand, str]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.captured_user_id: str | None = None

    async def do_handle_with_context(
        self,
        command: CreateItemCommand,
        context: DefaultExecutionContext,
    ) -> str:
        self.calls.append("do_handle_with_context")
        self.captured_user_id = context.user_id
        return f"created-{command.name}-by-{context.user_id}"


# ── handler with custom do_handle_with_context ────────────────


class DelegatingHandler(CommandHandler[CreateItemCommand, str]):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def do_handle(self, command: CreateItemCommand) -> str:
        self.calls.append("do_handle")
        return f"created-{command.name}"

    async def do_handle_with_context(
        self,
        command: CreateItemCommand,
        context: DefaultExecutionContext,
    ) -> str:
        self.calls.append("do_handle_with_context")
        return f"created-{command.name}-ctx"


# ── lifecycle hook order tests ────────────────────────────────


class TestCommandHandlerLifecycle:
    @pytest.mark.asyncio
    async def test_success_hook_order(self) -> None:
        handler = TrackingCommandHandler()
        result = await handler.handle(CreateItemCommand(name="foo"))

        assert result == "created-foo"
        assert handler.calls == ["pre_process", "do_handle", "post_process", "on_success"]

    @pytest.mark.asyncio
    async def test_on_error_called_on_exception(self) -> None:
        handler = FailingCommandHandler()

        with pytest.raises(ValueError, match="boom"):
            await handler.handle(CreateItemCommand())

        assert "pre_process" in handler.calls
        assert "do_handle" in handler.calls
        assert "on_error" in handler.calls
        assert "post_process" not in handler.calls
        assert "on_success" not in handler.calls

    @pytest.mark.asyncio
    async def test_error_hook_order(self) -> None:
        handler = FailingCommandHandler()

        with pytest.raises(ValueError):
            await handler.handle(CreateItemCommand())

        assert handler.calls == ["pre_process", "do_handle", "on_error"]

    @pytest.mark.asyncio
    async def test_map_error_transforms_exception(self) -> None:
        handler = MappingErrorCommandHandler()

        with pytest.raises(RuntimeError, match="mapped: original"):
            await handler.handle(CreateItemCommand())

        assert "on_error" in handler.calls
        assert "map_error" in handler.calls

    @pytest.mark.asyncio
    async def test_map_error_preserves_cause_chain(self) -> None:
        handler = MappingErrorCommandHandler()

        with pytest.raises(RuntimeError) as exc_info:
            await handler.handle(CreateItemCommand())

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, ValueError)


# ── handle_with_context tests ─────────────────────────────────


class TestCommandHandlerWithContext:
    @pytest.mark.asyncio
    async def test_handle_with_context_delegates_to_do_handle_with_context(self) -> None:
        handler = DelegatingHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        result = await handler.handle_with_context(CreateItemCommand(name="bar"), ctx)

        assert result == "created-bar-ctx"
        assert "do_handle_with_context" in handler.calls
        assert "do_handle" not in handler.calls

    @pytest.mark.asyncio
    async def test_default_do_handle_with_context_falls_back_to_do_handle(self) -> None:
        handler = TrackingCommandHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        result = await handler.handle_with_context(CreateItemCommand(name="baz"), ctx)

        assert result == "created-baz"
        assert "do_handle" in handler.calls

    @pytest.mark.asyncio
    async def test_handle_with_context_lifecycle_on_success(self) -> None:
        handler = TrackingCommandHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        await handler.handle_with_context(CreateItemCommand(), ctx)

        assert handler.calls == ["pre_process", "do_handle", "post_process", "on_success"]

    @pytest.mark.asyncio
    async def test_handle_with_context_lifecycle_on_error(self) -> None:
        handler = FailingCommandHandler()
        ctx = DefaultExecutionContext(user_id="user-1")

        with pytest.raises(ValueError):
            await handler.handle_with_context(CreateItemCommand(), ctx)

        assert handler.calls == ["pre_process", "do_handle", "on_error"]


# ── ContextAwareCommandHandler tests ──────────────────────────


class TestContextAwareCommandHandler:
    @pytest.mark.asyncio
    async def test_handle_without_context_raises_runtime_error(self) -> None:
        handler = ContextAwareCreateHandler()

        with pytest.raises(RuntimeError, match="requires an ExecutionContext"):
            await handler.handle(CreateItemCommand())

    @pytest.mark.asyncio
    async def test_handle_with_context_succeeds(self) -> None:
        handler = ContextAwareCreateHandler()
        ctx = DefaultExecutionContext(user_id="admin")

        result = await handler.handle_with_context(CreateItemCommand(name="gizmo"), ctx)

        assert result == "created-gizmo-by-admin"
        assert handler.captured_user_id == "admin"
        assert "do_handle_with_context" in handler.calls

    @pytest.mark.asyncio
    async def test_error_message_includes_class_name(self) -> None:
        handler = ContextAwareCreateHandler()

        with pytest.raises(RuntimeError, match="ContextAwareCreateHandler"):
            await handler.handle(CreateItemCommand())


# ── get_command_type tests ────────────────────────────────────


class TestGetCommandType:
    def test_resolves_generic_type_arg(self) -> None:
        handler = TrackingCommandHandler()
        assert handler.get_command_type() is CreateItemCommand

    def test_resolves_for_context_aware_handler(self) -> None:
        handler = ContextAwareCreateHandler()
        assert handler.get_command_type() is CreateItemCommand

    def test_base_handler_returns_none(self) -> None:
        handler = CommandHandler()
        assert handler.get_command_type() is None
