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
"""The SQL session registry: construction and wiring.

Its behavior runs on real databases in ``tests/integration/test_session_registry_matrix.py``, whose
sqlite-file lane is part of this fast suite.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
from pyfly.session.concurrency import AtomicSessionRegistry, SessionRegistry


def test_rejects_invalid_table_names() -> None:
    with pytest.raises(ValueError, match="table name"):
        PostgresSessionRegistry(lambda: object(), table="bad; DROP TABLE users")
    with pytest.raises(ValueError, match="table name"):
        PostgresSessionRegistry(lambda: object(), principals_table="bad; DROP TABLE users")


@pytest.mark.parametrize("ttl", [0, -5, timedelta(0)])
def test_rejects_a_ttl_that_is_not_positive(ttl: float | timedelta) -> None:
    """A registration renewed by a ttl of zero stays due: the purge would check the same batch forever."""
    with pytest.raises(ValueError, match="ttl"):
        PostgresSessionRegistry(lambda: object(), ttl=ttl)


def test_satisfies_the_registry_protocols() -> None:
    registry = PostgresSessionRegistry(lambda: object())
    assert isinstance(registry, SessionRegistry)
    assert isinstance(registry, AtomicSessionRegistry)


def test_provider_postgres_selection(tmp_path: Path) -> None:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.session.adapters.memory import InMemorySessionStore
    from pyfly.session.auto_configuration import SessionConcurrencyAutoConfiguration

    cfg = Config(
        {
            "pyfly": {
                "data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}"}},
                "session": {"concurrency": {"registry": "postgres"}},
            }
        }
    )
    controller = SessionConcurrencyAutoConfiguration().session_concurrency_controller(
        cfg, InMemorySessionStore(), Container()
    )
    assert isinstance(controller._registry, PostgresSessionRegistry)
