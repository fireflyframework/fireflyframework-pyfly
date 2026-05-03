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
"""Tests for HandlerRegistry with auto-discovery."""

from dataclasses import dataclass

import pytest

from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import command_handler, query_handler
from pyfly.cqrs.exceptions import CommandHandlerNotFoundException, QueryHandlerNotFoundException
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Command, Query

# -- Test messages ----------------------------------------------------------


@dataclass
class CreateUserCommand(Command[str]):
    name: str = ""


@dataclass
class DeleteUserCommand(Command[bool]):
    user_id: str = ""


@dataclass
class GetUserQuery(Query[dict]):
    user_id: str = ""


@dataclass
class ListUsersQuery(Query[list]):
    pass


# -- Test handlers ----------------------------------------------------------


class CreateUserHandler(CommandHandler[CreateUserCommand, str]):
    async def do_handle(self, command: CreateUserCommand) -> str:
        return f"created-{command.name}"


class DeleteUserHandler(CommandHandler[DeleteUserCommand, bool]):
    async def do_handle(self, command: DeleteUserCommand) -> bool:
        return True


class GetUserHandler(QueryHandler[GetUserQuery, dict]):
    async def do_handle(self, query: GetUserQuery) -> dict:
        return {"id": query.user_id, "name": "Alice"}


class ListUsersHandler(QueryHandler[ListUsersQuery, list]):
    async def do_handle(self, query: ListUsersQuery) -> list:
        return [{"id": "1", "name": "Alice"}]


# -- Decorated handlers for discover_from_beans ----------------------------


@command_handler
class DecoratedCreateHandler(CommandHandler[CreateUserCommand, str]):
    async def do_handle(self, command: CreateUserCommand) -> str:
        return f"decorated-{command.name}"


@query_handler
class DecoratedGetHandler(QueryHandler[GetUserQuery, dict]):
    async def do_handle(self, query: GetUserQuery) -> dict:
        return {"id": query.user_id}


class TestHandlerRegistry:
    @pytest.fixture
    def registry(self) -> HandlerRegistry:
        return HandlerRegistry()

    def test_register_and_find_command_handler(self, registry: HandlerRegistry) -> None:
        handler = CreateUserHandler()
        registry.register_command_handler(handler)
        found = registry.find_command_handler(CreateUserCommand)
        assert found is handler

    def test_register_and_find_query_handler(self, registry: HandlerRegistry) -> None:
        handler = GetUserHandler()
        registry.register_query_handler(handler)
        found = registry.find_query_handler(GetUserQuery)
        assert found is handler

    def test_command_handler_not_found_raises(self, registry: HandlerRegistry) -> None:
        with pytest.raises(CommandHandlerNotFoundException) as exc_info:
            registry.find_command_handler(CreateUserCommand)
        assert exc_info.value.command_type is CreateUserCommand
        assert "CreateUserCommand" in str(exc_info.value)

    def test_query_handler_not_found_raises(self, registry: HandlerRegistry) -> None:
        with pytest.raises(QueryHandlerNotFoundException) as exc_info:
            registry.find_query_handler(GetUserQuery)
        assert exc_info.value.query_type is GetUserQuery
        assert "GetUserQuery" in str(exc_info.value)

    def test_has_command_handler_true(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(CreateUserHandler())
        assert registry.has_command_handler(CreateUserCommand) is True

    def test_has_command_handler_false(self, registry: HandlerRegistry) -> None:
        assert registry.has_command_handler(CreateUserCommand) is False

    def test_has_query_handler_true(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(GetUserHandler())
        assert registry.has_query_handler(GetUserQuery) is True

    def test_has_query_handler_false(self, registry: HandlerRegistry) -> None:
        assert registry.has_query_handler(GetUserQuery) is False

    def test_unregister_command_handler(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(CreateUserHandler())
        assert registry.has_command_handler(CreateUserCommand) is True
        removed = registry.unregister_command_handler(CreateUserCommand)
        assert removed is True
        assert registry.has_command_handler(CreateUserCommand) is False

    def test_unregister_command_handler_missing_returns_false(self, registry: HandlerRegistry) -> None:
        removed = registry.unregister_command_handler(CreateUserCommand)
        assert removed is False

    def test_unregister_query_handler(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(GetUserHandler())
        assert registry.has_query_handler(GetUserQuery) is True
        removed = registry.unregister_query_handler(GetUserQuery)
        assert removed is True
        assert registry.has_query_handler(GetUserQuery) is False

    def test_unregister_query_handler_missing_returns_false(self, registry: HandlerRegistry) -> None:
        removed = registry.unregister_query_handler(GetUserQuery)
        assert removed is False

    def test_get_registered_command_types(self, registry: HandlerRegistry) -> None:
        registry.register_command_handler(CreateUserHandler())
        registry.register_command_handler(DeleteUserHandler())
        types = registry.get_registered_command_types()
        assert types == {CreateUserCommand, DeleteUserCommand}

    def test_get_registered_query_types(self, registry: HandlerRegistry) -> None:
        registry.register_query_handler(GetUserHandler())
        registry.register_query_handler(ListUsersHandler())
        types = registry.get_registered_query_types()
        assert types == {GetUserQuery, ListUsersQuery}

    def test_command_handler_count(self, registry: HandlerRegistry) -> None:
        assert registry.command_handler_count == 0
        registry.register_command_handler(CreateUserHandler())
        assert registry.command_handler_count == 1
        registry.register_command_handler(DeleteUserHandler())
        assert registry.command_handler_count == 2

    def test_query_handler_count(self, registry: HandlerRegistry) -> None:
        assert registry.query_handler_count == 0
        registry.register_query_handler(GetUserHandler())
        assert registry.query_handler_count == 1

    def test_register_replaces_existing_command_handler(self, registry: HandlerRegistry) -> None:
        handler1 = CreateUserHandler()
        handler2 = CreateUserHandler()
        registry.register_command_handler(handler1)
        registry.register_command_handler(handler2)
        assert registry.find_command_handler(CreateUserCommand) is handler2
        assert registry.command_handler_count == 1

    def test_discover_from_beans_command_handler(self, registry: HandlerRegistry) -> None:
        handler = DecoratedCreateHandler()
        registry.discover_from_beans([handler])
        assert registry.has_command_handler(CreateUserCommand) is True
        assert registry.find_command_handler(CreateUserCommand) is handler

    def test_discover_from_beans_query_handler(self, registry: HandlerRegistry) -> None:
        handler = DecoratedGetHandler()
        registry.discover_from_beans([handler])
        assert registry.has_query_handler(GetUserQuery) is True
        assert registry.find_query_handler(GetUserQuery) is handler

    def test_discover_from_beans_mixed(self, registry: HandlerRegistry) -> None:
        cmd_handler = DecoratedCreateHandler()
        qry_handler = DecoratedGetHandler()
        plain_object = "not-a-handler"

        registry.discover_from_beans([cmd_handler, qry_handler, plain_object])

        assert registry.command_handler_count == 1
        assert registry.query_handler_count == 1

    def test_discover_from_beans_ignores_non_handlers(self, registry: HandlerRegistry) -> None:
        registry.discover_from_beans([42, "hello", None, object()])
        assert registry.command_handler_count == 0
        assert registry.query_handler_count == 0

    def test_discover_from_beans_ignores_undecorated_handlers(self, registry: HandlerRegistry) -> None:
        undecorated = CreateUserHandler()
        registry.discover_from_beans([undecorated])
        assert registry.command_handler_count == 0
