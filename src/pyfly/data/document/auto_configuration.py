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
"""Document data layer (MongoDB/Beanie) auto-configuration."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = object  # type: ignore[misc,assignment]

from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.data.document.mongodb.initializer import BeanieInitializer
from pyfly.data.document.mongodb.post_processor import (
    MongoRepositoryBeanPostProcessor,
)


@auto_configuration
@conditional_on_class("beanie")
@conditional_on_property("pyfly.data.document.enabled", having_value="true")
class DocumentAutoConfiguration:
    """Auto-configures Motor client, Beanie initializer, and Mongo repository post-processor."""

    @bean
    def motor_client(self, config: Config) -> AsyncIOMotorClient:  # type: ignore[type-arg]
        uri = str(config.get("pyfly.data.document.uri", "mongodb://localhost:27017"))
        return AsyncIOMotorClient(uri)

    @bean
    def mongo_post_processor(self) -> MongoRepositoryBeanPostProcessor:
        return MongoRepositoryBeanPostProcessor()

    @bean
    def odm_initializer(
        self,
        config: Config,
        container: Container,
        motor_client: AsyncIOMotorClient,  # type: ignore[type-arg]
    ) -> BeanieInitializer:
        return BeanieInitializer(
            motor_client=motor_client,
            config=config,
            container=container,
        )
