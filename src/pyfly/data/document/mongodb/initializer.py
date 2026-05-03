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
"""Beanie ODM initializer — lifecycle bean that initializes Beanie on start."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorClient

    from pyfly.container.container import Container
    from pyfly.core.config import Config


class BeanieInitializer:
    """Lifecycle bean that initializes Beanie ODM during infrastructure startup.

    Scans the container for BaseDocument subclasses and calls
    ``init_beanie()`` with the Motor database and discovered models.
    """

    def __init__(
        self,
        motor_client: AsyncIOMotorClient[Any],
        config: Config,
        container: Container,
    ) -> None:
        self._motor_client = motor_client
        self._config = config
        self._container = container

    async def start(self) -> None:
        from pyfly.data.document.mongodb.document import BaseDocument
        from pyfly.data.document.mongodb.repository import MongoRepository

        db_name = str(self._config.get("pyfly.data.document.database", "pyfly"))

        document_models: list[type] = []

        # Primary: discover from MongoRepository._entity_type (set by __init_subclass__)
        for cls in self._container._registrations:
            if isinstance(cls, type) and issubclass(cls, MongoRepository) and cls is not MongoRepository:
                entity_type = getattr(cls, "_entity_type", None)
                if (
                    entity_type is not None
                    and isinstance(entity_type, type)
                    and issubclass(entity_type, BaseDocument)
                    and entity_type not in document_models
                ):
                    document_models.append(entity_type)

        # Secondary: directly registered BaseDocument subclasses (e.g. standalone models)
        for cls in self._container._registrations:
            if (
                isinstance(cls, type)
                and issubclass(cls, BaseDocument)
                and cls is not BaseDocument
                and cls not in document_models
            ):
                document_models.append(cls)

        if document_models:
            from beanie import init_beanie

            await init_beanie(
                database=self._motor_client[db_name],  # type: ignore[arg-type]
                document_models=document_models,
            )

    async def stop(self) -> None:
        self._motor_client.close()
