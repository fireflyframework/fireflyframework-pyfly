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
"""Run-on-startup database migrations (Spring Boot Flyway-style auto-migrate).

When ``pyfly.data.relational.migrations.enabled=true``, applies ``alembic upgrade head`` at
startup, reusing the project's existing Alembic setup (``alembic.ini`` + ``alembic/env.py``
created by ``pyfly db init``). The upgrade runs in a worker thread because the generated
async ``env.py`` calls ``asyncio.run`` internally — which must not be nested inside the
running event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class MigrationRunner:
    """Startup lifecycle adapter that applies Alembic migrations once on ``start()``."""

    def __init__(self, *, url: str = "", config_path: str = "alembic.ini", revision: str = "head") -> None:
        self._url = url
        self._config_path = config_path
        self._revision = revision

    async def start(self) -> None:
        if not os.path.exists(self._config_path):
            logger.warning(
                "pyfly.data.relational.migrations.enabled is true but %s was not found — "
                "run 'pyfly db init' to create the Alembic environment; skipping migrations.",
                self._config_path,
            )
            return
        await asyncio.to_thread(self._upgrade)
        logger.info("Database migrations applied (alembic upgrade %s)", self._revision)

    def _upgrade(self) -> None:
        from alembic import command
        from alembic.config import Config as AlembicConfig

        cfg = AlembicConfig(self._config_path)
        if self._url:
            # Single source of truth: migrate the same datasource the app uses.
            cfg.set_main_option("sqlalchemy.url", self._url)
        command.upgrade(cfg, self._revision)

    async def stop(self) -> None:
        return None
