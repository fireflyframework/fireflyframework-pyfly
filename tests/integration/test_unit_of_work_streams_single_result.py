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
"""The single-result rules of ``test_unit_of_work_streams``, in the fast suite (WP01-05).

MySQL and MariaDB have one active result per connection; the unit holds to that rule wherever its
transaction manager says so (``TransactionCapabilities.multiple_active_results`` false). Here SQLite runs
the same tests with the capability turned off, so the refusal, the release of an exhausted or closed
stream and the close of an abandoned one are checked on every fast run, not only on the server lanes.
"""

from __future__ import annotations

import dataclasses

import pytest

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionCapabilities
from tests.integration.test_unit_of_work_streams import (  # noqa: F401 — the tests and fixtures run here too
    harness,
    test_a_sibling_write_beside_an_open_stream,
    test_a_statement_between_two_fetches_of_the_same_task,
    test_a_stream_abandoned_open_does_not_break_the_commit,
    test_a_stream_abandoned_open_does_not_break_the_rollback,
    test_a_stream_closed_early_frees_the_connection,
    test_a_stream_of_its_own_closed_early_frees_its_connection,
    test_an_exhausted_stream_frees_the_connection,
)
from tests.support.backend_matrix import SQLITE_FILE

pytestmark = pytest.mark.backends(SQLITE_FILE)


@pytest.fixture
def single_result(monkeypatch: pytest.MonkeyPatch) -> bool:
    """SQLite's connection, told it has one active result at a time."""
    original = SqlAlchemyTransactionManager.capabilities

    def capabilities(self: SqlAlchemyTransactionManager) -> TransactionCapabilities:
        assert original.fget is not None
        return dataclasses.replace(original.fget(self), multiple_active_results=False)

    monkeypatch.setattr(SqlAlchemyTransactionManager, "capabilities", property(capabilities))
    return True
