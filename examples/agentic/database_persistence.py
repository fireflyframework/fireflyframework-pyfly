#!/usr/bin/env python3
# Copyright 2026 Firefly Software Foundation
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

"""Native PyFly Data working memory, including a cold application restart.

Install agentic-relational. By default this uses an isolated temporary SQLite
file and makes no provider calls. For PostgreSQL select agentic-postgres and
PYFLY_DATA_RELATIONAL_URL; configure schema creation explicitly through
PYFLY_DATA_RELATIONAL_DDL_AUTO. Existing schemas are validated by default.
"""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.agentic._persistence import exercise_persistence
from pyfly.core.config import Config


def application_config(url: str, *, ddl_auto: str) -> Config:
    return Config(
        {
            "pyfly": {
                "data": {"relational": {"enabled": True, "url": url, "ddl-auto": ddl_auto}},
                "agentic": {
                    "enabled": True,
                    "memory_backend": "relational",
                    "plugin_auto_discover": False,
                    "observability_enabled": False,
                },
            }
        }
    )


async def main() -> None:
    url = os.environ.get("PYFLY_DATA_RELATIONAL_URL")
    if url:
        await exercise_persistence(
            application_config(url, ddl_auto=os.environ.get("PYFLY_DATA_RELATIONAL_DDL_AUTO", "validate"))
        )
    else:
        with TemporaryDirectory(prefix="pyfly-memory-example-") as directory:
            url = f"sqlite+aiosqlite:///{Path(directory) / 'memory.db'}"
            await exercise_persistence(application_config(url, ddl_auto="create"))


if __name__ == "__main__":
    asyncio.run(main())
