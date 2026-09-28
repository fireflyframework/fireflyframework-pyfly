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
        assert types == {"on_class"}  # active with Beanie: the wiring check runs even when the layer is off
        client_conditions = getattr(DocumentAutoConfiguration.mongo_client, "__pyfly_conditions__", [])
        assert {
            "type": "on_property",
            "key": "pyfly.data.document.enabled",
            "having_value": "true",
            "match_if_missing": False,
        } in client_conditions

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

    def test_discovers_every_document_the_repositories_and_links_reach(self) -> None:
        """C035: any Beanie document (not only BaseDocument subclasses), the documents their Link fields name, and
        the configured models, so none fails at first use with CollectionWasNotInitialized."""
        from beanie import BackLink, Document, Link
        from pymongo import AsyncMongoClient

        from pyfly.container.container import Container
        from pyfly.data.document.mongodb.document import BaseDocument
        from pyfly.data.document.mongodb.initializer import BeanieInitializer
        from pyfly.data.document.mongodb.repository import MongoRepository

        class DiscoveryAuthor(Document):
            name: str

        class DiscoveryTag(BaseDocument):
            label: str

        class DiscoveryBook(Document):
            title: str
            author: Link[DiscoveryAuthor] | None = None
            tags: list[Link[DiscoveryTag]] = []

        class DiscoveryShelf(Document):
            books: list[BackLink[DiscoveryBook]] = []

        class DiscoveryDoc(BaseDocument):
            name: str

        class DiscoveryBookRepo(MongoRepository[DiscoveryBook, str]):
            pass

        class DiscoveryDocRepo(MongoRepository[DiscoveryDoc, str]):
            pass

        container = Container()
        container.register(DiscoveryBookRepo)
        container.register(DiscoveryDocRepo)
        config = Config(
            {
                "pyfly": {
                    "data": {
                        "document": {
                            "database": "testdb",
                            "models": ["tests.data.test_mongo_wiring.ConfiguredDocument"],
                        }
                    }
                }
            }
        )
        client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)
        initializer = BeanieInitializer(motor_client=client, config=config, container=container)
        found = initializer.discover()
        assert set(found) == {
            DiscoveryBook,
            DiscoveryAuthor,
            DiscoveryTag,
            DiscoveryDoc,
            ConfiguredDocument,
        }
        assert DiscoveryShelf not in found  # nothing names it

    def test_a_configured_model_that_is_not_a_document_fails(self) -> None:
        from pymongo import AsyncMongoClient

        from pyfly.container.container import Container
        from pyfly.data.document.mongodb.initializer import BeanieInitializer

        config = Config({"pyfly": {"data": {"document": {"models": ["tests.data.test_mongo_wiring.Config"]}}}})
        client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)
        initializer = BeanieInitializer(motor_client=client, config=config, container=Container())
        with pytest.raises(ValueError, match="neither a Beanie document class nor a module"):
            initializer.discover()


try:
    from beanie import Document as _Document

    class ConfiguredDocument(_Document):
        """A document named only by pyfly.data.document.models."""

        name: str
except ImportError:  # pragma: no cover
    pass


def test_the_replica_set_container_raises_the_open_file_limit() -> None:
    """A suite that gives every test a database of its own exhausted mongod's default 1024 open files, and
    WiredTiger aborted the server; the replica-set fixture runs it with MongoDB's recommended limit."""
    pytest.importorskip("testcontainers")
    from pyfly.testing.testcontainers import mongodb_replica_set_container

    container = mongodb_replica_set_container()
    (ulimit,) = container.get_wrapped_container()._kwargs["ulimits"]
    assert (ulimit.name, ulimit.soft, ulimit.hard) == ("nofile", 64000, 64000)
