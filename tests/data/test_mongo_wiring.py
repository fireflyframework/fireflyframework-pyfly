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
"""Tests for MongoDB auto-configuration wiring."""

from __future__ import annotations

import pytest

from pyfly.container.exceptions import NoSuchBeanError
from pyfly.core.config import Config


class TestDocumentAutoConfiguration:
    def test_produces_mongo_client(self) -> None:
        from pymongo import AsyncMongoClient

        from pyfly.data.document.auto_configuration import DocumentAutoConfiguration

        config = Config(
            {
                "pyfly": {
                    "data": {
                        "document": {
                            "enabled": True,
                            "uri": "mongodb://localhost:27017",
                            "database": "testdb",
                        }
                    }
                }
            }
        )
        instance = DocumentAutoConfiguration()
        client = instance.mongo_client(config)
        assert isinstance(client, AsyncMongoClient)

    def test_produces_post_processor(self) -> None:
        from pyfly.data.document.auto_configuration import DocumentAutoConfiguration
        from pyfly.data.document.mongodb.post_processor import (
            MongoRepositoryBeanPostProcessor,
        )

        instance = DocumentAutoConfiguration()
        pp = instance.mongo_post_processor()
        assert isinstance(pp, MongoRepositoryBeanPostProcessor)

    def test_has_correct_conditions(self) -> None:
        from pyfly.data.document.auto_configuration import DocumentAutoConfiguration

        conditions = getattr(DocumentAutoConfiguration, "__pyfly_conditions__", [])
        types = {c["type"] for c in conditions}
        assert "on_class" in types
        assert "on_property" in types

    def test_context_registers_motor_and_pp(self) -> None:
        """Auto-configuration registers pymongo AsyncMongoClient + post-processor in the container."""
        from pymongo import AsyncMongoClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.data.document.mongodb.initializer import BeanieInitializer
        from pyfly.data.document.mongodb.post_processor import (
            MongoRepositoryBeanPostProcessor,
        )

        config = Config(
            {
                "pyfly": {
                    "data": {
                        "document": {
                            "enabled": True,
                            "uri": "mongodb://localhost:27017",
                            "database": "testdb",
                        }
                    }
                }
            }
        )
        ctx = ApplicationContext(config)

        # Run the auto-configuration phases (without full lifecycle which requires MongoDB)
        ctx._register_auto_configurations()
        ctx._filter_by_profile()
        ctx._evaluate_conditions()
        ctx._process_configurations(auto=False)
        ctx._evaluate_bean_conditions()
        ctx._process_configurations(auto=True)

        mongo_client = ctx.get_bean(AsyncMongoClient)
        assert mongo_client is not None
        assert isinstance(mongo_client, AsyncMongoClient)

        pp = ctx.get_bean(MongoRepositoryBeanPostProcessor)
        assert pp is not None

        initializer = ctx.get_bean(BeanieInitializer)
        assert initializer is not None
        assert hasattr(initializer, "start")
        assert hasattr(initializer, "stop")

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self) -> None:
        from pymongo import AsyncMongoClient

        from pyfly.context.application_context import ApplicationContext

        config = Config({"pyfly": {"data": {"document": {"enabled": False}}}})
        ctx = ApplicationContext(config)
        await ctx.start()
        try:
            with pytest.raises(NoSuchBeanError):
                ctx.get_bean(AsyncMongoClient)
        finally:
            await ctx.stop()


class TestBeanieInitializer:
    def test_produces_initializer(self) -> None:
        """DocumentAutoConfiguration produces a BeanieInitializer bean."""
        from pyfly.data.document.auto_configuration import DocumentAutoConfiguration
        from pyfly.data.document.mongodb.initializer import BeanieInitializer

        instance = DocumentAutoConfiguration()
        config = Config(
            {
                "pyfly": {
                    "data": {
                        "document": {
                            "enabled": True,
                            "uri": "mongodb://localhost:27017",
                            "database": "testdb",
                        }
                    }
                }
            }
        )
        motor_client = instance.mongo_client(config)

        from pyfly.container.container import Container

        container = Container()
        initializer = instance.odm_initializer(config, container, motor_client)
        assert isinstance(initializer, BeanieInitializer)
        assert hasattr(initializer, "start")
        assert hasattr(initializer, "stop")

    @pytest.mark.asyncio
    async def test_discovers_document_models_from_repositories(self) -> None:
        """BeanieInitializer discovers document models via MongoRepository._entity_type."""
        from unittest.mock import AsyncMock, patch

        from pyfly.container.container import Container
        from pyfly.data.document.mongodb.document import BaseDocument
        from pyfly.data.document.mongodb.initializer import BeanieInitializer
        from pyfly.data.document.mongodb.repository import MongoRepository

        class DiscoveryDoc(BaseDocument):
            name: str

            class Settings:
                name = "discovery_docs"

        class DiscoveryDocRepo(MongoRepository[DiscoveryDoc, str]):
            pass

        container = Container()
        container.register(DiscoveryDocRepo)

        from mongomock_motor import AsyncMongoMockClient

        client = AsyncMongoMockClient()
        config = Config({"pyfly": {"data": {"document": {"database": "testdb"}}}})
        initializer = BeanieInitializer(motor_client=client, config=config, container=container)

        with patch("beanie.init_beanie", new_callable=AsyncMock) as mock_init:
            await initializer.start()
            mock_init.assert_called_once()
            call_kwargs = mock_init.call_args
            models = call_kwargs.kwargs.get("document_models") or call_kwargs[1].get("document_models")
            assert DiscoveryDoc in models

        client.close()
