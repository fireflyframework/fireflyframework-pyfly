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
"""Tests for enhanced @command_handler and @query_handler decorators."""

from pyfly.cqrs.decorators import command_handler, query_handler

# -- Bare decorator usage ---------------------------------------------------


@command_handler
class BareCommandHandler:
    pass


@query_handler
class BareQueryHandler:
    pass


# -- Parameterized decorator usage ------------------------------------------


@command_handler(
    timeout=30,
    retries=3,
    backoff_ms=500,
    metrics=False,
    tracing=False,
    validation=False,
    priority=10,
    tags=("critical", "order"),
    description="Creates an order",
)
class ParameterizedCommandHandler:
    pass


@query_handler(
    timeout=15,
    retries=1,
    metrics=True,
    tracing=False,
    cacheable=True,
    cache_ttl=600,
    cache_key_prefix="orders",
    priority=5,
    tags=("read",),
    description="Gets an order",
)
class ParameterizedQueryHandler:
    pass


# -- Tests ------------------------------------------------------------------


class TestCommandHandlerDecorator:
    def test_bare_sets_handler_type(self) -> None:
        assert BareCommandHandler.__pyfly_handler_type__ == "command"

    def test_bare_sets_default_timeout(self) -> None:
        assert BareCommandHandler.__pyfly_timeout__ is None

    def test_bare_sets_default_retries(self) -> None:
        assert BareCommandHandler.__pyfly_retries__ == 0

    def test_bare_sets_default_backoff_ms(self) -> None:
        assert BareCommandHandler.__pyfly_backoff_ms__ == 1000

    def test_bare_sets_default_metrics(self) -> None:
        assert BareCommandHandler.__pyfly_metrics__ is True

    def test_bare_sets_default_tracing(self) -> None:
        assert BareCommandHandler.__pyfly_tracing__ is True

    def test_bare_sets_default_validation(self) -> None:
        assert BareCommandHandler.__pyfly_validation__ is True

    def test_bare_sets_default_priority(self) -> None:
        assert BareCommandHandler.__pyfly_priority__ == 0

    def test_bare_sets_default_tags(self) -> None:
        assert BareCommandHandler.__pyfly_tags__ == ()

    def test_bare_sets_default_description(self) -> None:
        assert BareCommandHandler.__pyfly_description__ == ""

    def test_parameterized_sets_handler_type(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_handler_type__ == "command"

    def test_parameterized_sets_timeout(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_timeout__ == 30

    def test_parameterized_sets_retries(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_retries__ == 3

    def test_parameterized_sets_backoff_ms(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_backoff_ms__ == 500

    def test_parameterized_sets_metrics(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_metrics__ is False

    def test_parameterized_sets_tracing(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_tracing__ is False

    def test_parameterized_sets_validation(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_validation__ is False

    def test_parameterized_sets_priority(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_priority__ == 10

    def test_parameterized_sets_tags(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_tags__ == ("critical", "order")

    def test_parameterized_sets_description(self) -> None:
        assert ParameterizedCommandHandler.__pyfly_description__ == "Creates an order"

    def test_decorated_class_is_same_class(self) -> None:
        assert BareCommandHandler.__name__ == "BareCommandHandler"

    def test_decorator_does_not_alter_class_bases(self) -> None:
        assert BareCommandHandler.__bases__ == (object,)


class TestQueryHandlerDecorator:
    def test_bare_sets_handler_type(self) -> None:
        assert BareQueryHandler.__pyfly_handler_type__ == "query"

    def test_bare_sets_default_timeout(self) -> None:
        assert BareQueryHandler.__pyfly_timeout__ is None

    def test_bare_sets_default_retries(self) -> None:
        assert BareQueryHandler.__pyfly_retries__ == 0

    def test_bare_sets_default_metrics(self) -> None:
        assert BareQueryHandler.__pyfly_metrics__ is True

    def test_bare_sets_default_tracing(self) -> None:
        assert BareQueryHandler.__pyfly_tracing__ is True

    def test_bare_sets_default_cacheable(self) -> None:
        assert BareQueryHandler.__pyfly_cacheable__ is False

    def test_bare_sets_default_cache_ttl(self) -> None:
        assert BareQueryHandler.__pyfly_cache_ttl__ is None

    def test_bare_sets_default_cache_key_prefix(self) -> None:
        assert BareQueryHandler.__pyfly_cache_key_prefix__ is None

    def test_bare_sets_default_priority(self) -> None:
        assert BareQueryHandler.__pyfly_priority__ == 0

    def test_bare_sets_default_tags(self) -> None:
        assert BareQueryHandler.__pyfly_tags__ == ()

    def test_bare_sets_default_description(self) -> None:
        assert BareQueryHandler.__pyfly_description__ == ""

    def test_parameterized_sets_handler_type(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_handler_type__ == "query"

    def test_parameterized_sets_timeout(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_timeout__ == 15

    def test_parameterized_sets_retries(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_retries__ == 1

    def test_parameterized_sets_cacheable(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_cacheable__ is True

    def test_parameterized_sets_cache_ttl(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_cache_ttl__ == 600

    def test_parameterized_sets_cache_key_prefix(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_cache_key_prefix__ == "orders"

    def test_parameterized_sets_priority(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_priority__ == 5

    def test_parameterized_sets_tags(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_tags__ == ("read",)

    def test_parameterized_sets_description(self) -> None:
        assert ParameterizedQueryHandler.__pyfly_description__ == "Gets an order"

    def test_decorated_class_is_same_class(self) -> None:
        assert BareQueryHandler.__name__ == "BareQueryHandler"

    def test_supports_caching_on_handler_instance(self) -> None:
        from pyfly.cqrs.query.handler import QueryHandler as RichQueryHandler

        @query_handler(cacheable=True, cache_ttl=600)
        class CacheableHandler(RichQueryHandler):
            async def do_handle(self, query):
                return None

        handler = CacheableHandler()
        assert handler.supports_caching() is True
        assert handler.get_cache_ttl_seconds() == 600

    def test_no_caching_on_bare_handler_instance(self) -> None:
        from pyfly.cqrs.query.handler import QueryHandler as RichQueryHandler

        @query_handler
        class NoCacheHandler(RichQueryHandler):
            async def do_handle(self, query):
                return None

        handler = NoCacheHandler()
        assert handler.supports_caching() is False
        assert handler.get_cache_ttl_seconds() is None
