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
"""``StatementCounter`` on a sync engine and its verb parsing (the async lanes are in
``tests/integration/test_statement_counter_matrix.py``)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from pyfly.testing import StatementCounter
from pyfly.testing.statement_counter import statement_verb


@pytest.mark.parametrize(
    ("sql", "verb"),
    [
        ("SELECT 1", "SELECT"),
        ("  select * from t", "SELECT"),
        ("\n\tINSERT INTO t VALUES (1)", "INSERT"),
        ("(SELECT 1) UNION (SELECT 2)", "SELECT"),
        ("-- a comment\nUPDATE t SET a = 1", "UPDATE"),
        ("/* hint */ DELETE FROM t", "DELETE"),
        ("/* one */ -- two\n ( ( select 1 ) )", "SELECT"),
        ("WITH q AS (SELECT 1) SELECT * FROM q", "WITH"),
        ("PRAGMA foreign_keys=ON", "PRAGMA"),
        ("SAVEPOINT sa_savepoint_1", "SAVEPOINT"),
        ("", ""),
        ("-- only a comment", ""),
    ],
)
def test_statement_verb(sql: str, verb: str) -> None:
    assert statement_verb(sql) == verb


def test_counts_a_sync_engine(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
    try:
        with StatementCounter(engine) as counter, engine.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO t (id) VALUES (1), (2)"))
            conn.execute(text("SELECT id FROM t"))
    finally:
        engine.dispose()
    assert counter.counts() == {"CREATE": 1, "INSERT": 1, "SELECT": 1}
    assert counter.commits == 1


def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'idempotent.db'}")
    try:
        counter = StatementCounter(engine).start().start()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        counter.stop()
        counter.stop()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    assert counter.count() == 1  # one listener, attached once, removed once


def test_importing_the_counter_does_not_import_sqlalchemy() -> None:
    code = "import sys, pyfly.testing.statement_counter; print('sqlalchemy' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"
