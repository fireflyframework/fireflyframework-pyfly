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

"""Memory on the application's shared native PyFly Data MongoDB client.

Install agentic-mongodb. PYFLY_DATA_DOCUMENT_URI must select a disposable replica
set; PYFLY_DATA_DOCUMENT_DATABASE selects its existing memory database (default
firefly_memory). Explicitly enable PYFLY_AGENTIC_MEMORY_CREATE_INDEXES=true for a
new collection. Old global entry-ID indexes require the documented migration.
This example makes no hosted model requests and never prints connection URLs.
"""

import asyncio
import os

from examples.agentic._persistence import exercise_persistence
from pyfly.core.config import Config


async def main() -> None:
    uri = os.environ.get("PYFLY_DATA_DOCUMENT_URI")
    if not uri:
        raise SystemExit("Set PYFLY_DATA_DOCUMENT_URI to a disposable MongoDB replica set.")
    database = os.environ.get("PYFLY_DATA_DOCUMENT_DATABASE", "firefly_memory")
    config = Config(
        {
            "pyfly": {
                "data": {"document": {"enabled": True, "uri": uri, "database": database}},
                "agentic": {
                    "enabled": True,
                    "memory_backend": "mongodb",
                    "memory_mongodb_database": database,
                    "plugin_auto_discover": False,
                    "observability_enabled": False,
                },
            }
        }
    )
    await exercise_persistence(config)


if __name__ == "__main__":
    asyncio.run(main())
