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
"""Run-on-startup Alembic migrations (v26.06.61), on real SQLite files.

- C028: the ``env.py`` of ``pyfly db init`` ran ``fileConfig(alembic.ini)`` inside the application: every
  existing logger was disabled, PyFly's handlers removed and root set to WARNING.
- C119/C120: the runner fed the URL through Alembic's configuration parser, so any ``%`` (a percent-encoded
  password, a path) aborted the start.
- The migrations run on the application's engine (its SQLite pragmas included), with foreign keys off for
  Alembic's batch rebuilds and ``PRAGMA foreign_key_check`` before the commit.
- C095: a batch rebuild on SQLite silently dropped an unnamed ``CHECK`` constraint.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from pyfly.core.config import Config
from pyfly.data.relational.migrations import MigrationRunner

# The env.py `pyfly db init` generated before 26.09.08: it opens its own engine from alembic.ini's URL and
# loads alembic.ini's logging configuration. Projects keep it until they regenerate it.
LEGACY_ENV_PY = """\
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from pyfly.data.relational.sqlalchemy import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


import asyncio

asyncio.run(run_migrations_online())
"""

# What `alembic init` writes after [alembic]: the logging configuration fileConfig applies.
_INI_LOGGING = """
[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %%(levelname)-5.5s [%%(name)s] %%(message)s
"""


def _revision(number: int, body: str, previous: str | None) -> str:
    return (
        f"import sqlalchemy as sa\nfrom alembic import op\n\nrevision = 'wp11r{number:03d}'\n"
        f"down_revision = {previous!r}\nbranch_labels = None\ndepends_on = None\n\n\n"
        f"def upgrade() -> None:\n{body}\n"
    )


def environment(root: Path, revisions: list[str], *, env_py: str | None = None, url: str | None = None) -> Path:
    """An Alembic environment in *root* (``alembic.ini`` with ``alembic init``'s logging sections, and the
    ``env.py`` of ``pyfly db init`` unless *env_py* is given), one revision per body in *revisions*."""
    from pyfly.cli.db import _ENV_PY_TEMPLATE

    migrations = root / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (migrations / "env.py").write_text(env_py if env_py is not None else _ENV_PY_TEMPLATE)
    previous: str | None = None
    for number, body in enumerate(revisions, start=1):
        (migrations / "versions" / f"wp11r{number:03d}.py").write_text(_revision(number, body, previous))
        previous = f"wp11r{number:03d}"
    ini = root / "alembic.ini"
    main = f"[alembic]\nscript_location = {migrations}\n"
    if url is not None:
        main += f"sqlalchemy.url = {url.replace('%', '%%')}\n"
    ini.write_text(main + _INI_LOGGING)
    return ini


_CREATE_ITEMS = "    op.create_table('wp11_mig_item', sa.Column('id', sa.Integer(), primary_key=True))"


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


def _registry_engine(url: str) -> tuple[Any, AsyncEngine]:
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    registry = DataSourceRegistry(Config({"pyfly": {"data": {"relational": {"url": url}}}}))
    return registry, registry.primary.engine


# ---------------------------------------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_noop_when_alembic_ini_missing(tmp_path: Path) -> None:
    runner = MigrationRunner(config_path=str(tmp_path / "missing.ini"))
    await runner.start()  # must not raise — logs a warning and skips


@pytest.mark.asyncio
async def test_start_runs_upgrade_when_ini_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = migrations\n")
    calls: list[tuple[str, str]] = []

    import alembic.command

    def _fake_upgrade(cfg: Any, revision: str) -> None:
        calls.append((cfg.get_main_option("sqlalchemy.url"), revision))

    monkeypatch.setattr(alembic.command, "upgrade", _fake_upgrade)

    runner = MigrationRunner(url="sqlite+aiosqlite:///app.db", config_path=str(ini), revision="head")
    await runner.start()

    assert calls == [("sqlite+aiosqlite:///app.db", "head")]  # migrates the app's datasource


@pytest.mark.asyncio
async def test_runner_has_lifecycle_methods() -> None:
    runner = MigrationRunner()
    assert callable(runner.start) and callable(runner.stop)
    await runner.stop()  # no-op


def test_migration_auto_configuration_builds_runner() -> None:
    from pyfly.data.relational.auto_configuration import MigrationAutoConfiguration

    cfg = Config(
        {"pyfly": {"data": {"relational": {"url": "sqlite+aiosqlite:///app.db", "migrations": {"enabled": "true"}}}}}
    )
    runner = MigrationAutoConfiguration().migration_runner(cfg)
    assert isinstance(runner, MigrationRunner)


# ---------------------------------------------------------------------------------------------------------
# The application's logging survives the migrations (C028)
# ---------------------------------------------------------------------------------------------------------


class _Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def application_logging() -> Iterator[_Recorder]:
    """Logging as an application configured it: a handler on root at INFO, and a module logger created before
    the start. Restored afterwards, whatever a migration did to it."""
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), logging.Logger.manager.loggerDict.copy())
    disabled = {name: logger.disabled for name, logger in saved[2].items() if isinstance(logger, logging.Logger)}
    recorder = _Recorder()
    root.addHandler(recorder)
    root.setLevel(logging.INFO)
    logging.getLogger("wp11.app.service")
    try:
        yield recorder
    finally:
        root.handlers[:] = saved[1]
        root.setLevel(saved[0])
        for name, logger in logging.Logger.manager.loggerDict.items():
            if isinstance(logger, logging.Logger):
                logger.disabled = disabled.get(name, False)


def _assert_logging_intact(recorder: _Recorder) -> None:
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert recorder in root.handlers
    assert logging.getLogger("wp11.app.service").disabled is False
    assert logging.getLogger("pyfly.data.relational.migrations").disabled is False
    logging.getLogger("wp11.app.service").error("after the migrations")
    assert any(record.getMessage() == "after the migrations" for record in recorder.records)


@pytest.mark.asyncio
async def test_startup_migrations_keep_the_application_logging(tmp_path: Path, application_logging: _Recorder) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ini = environment(tmp_path, [_CREATE_ITEMS])
    registry, engine = _registry_engine(url)
    try:
        await MigrationRunner(config_path=str(ini), engine=engine).start()
    finally:
        await registry.close()
    assert "wp11_mig_item" in await _tables(url)
    _assert_logging_intact(application_logging)
    assert any("Database migrations applied" in record.getMessage() for record in application_logging.records)


@pytest.mark.asyncio
async def test_an_env_py_generated_before_keeps_the_application_logging_too(
    tmp_path: Path, application_logging: _Recorder
) -> None:
    """An env.py that calls fileConfig(config.config_file_name) never sees the file name from the runner."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ini = environment(tmp_path, [_CREATE_ITEMS], env_py=LEGACY_ENV_PY)
    await MigrationRunner(url=url, config_path=str(ini)).start()
    assert "wp11_mig_item" in await _tables(url)
    _assert_logging_intact(application_logging)


# ---------------------------------------------------------------------------------------------------------
# A '%' in the URL (C119, C120)
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("env_py", ["pyfly", "legacy"])
async def test_a_percent_sign_in_the_url_is_migrated(tmp_path: Path, env_py: str) -> None:
    """A percent-encoded character (an '@' in a password, here in a directory name) reaches the database as the
    application's engine reads the URL, with the env.py of today and with one generated before."""
    from sqlalchemy.engine import make_url

    url = f"sqlite+aiosqlite:///{tmp_path / 'p%40ss' / 'app.db'}"
    # The directory the engine opens (SQLAlchemy 2.1 decodes the path, 2.0 keeps it as written).
    Path(str(make_url(url).database)).parent.mkdir()
    ini = environment(tmp_path, [_CREATE_ITEMS], env_py=LEGACY_ENV_PY if env_py == "legacy" else None)
    await MigrationRunner(url=url, config_path=str(ini)).start()
    assert "wp11_mig_item" in await _tables(url)


@pytest.mark.asyncio
async def test_the_migrations_run_on_the_application_engine(tmp_path: Path) -> None:
    """With the application's engine, the runner hands env.py one of its connections: the migrations see the
    application's database whatever alembic.ini says."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ini = environment(tmp_path, [_CREATE_ITEMS], url=f"sqlite+aiosqlite:///{tmp_path / 'elsewhere.db'}")
    registry, engine = _registry_engine(url)
    try:
        await MigrationRunner(config_path=str(ini), engine=engine).start()
    finally:
        await registry.close()
    assert "wp11_mig_item" in await _tables(url)
    assert not (tmp_path / "elsewhere.db").exists()


# ---------------------------------------------------------------------------------------------------------
# SQLite: foreign keys and batch rebuilds (C095)
# ---------------------------------------------------------------------------------------------------------

_PARENT_AND_CHILD = """\
    op.create_table('wp11_owner', sa.Column('id', sa.Integer(), primary_key=True))
    op.create_table(
        'wp11_account',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('wp11_owner.id', ondelete='CASCADE')),
        sa.Column('name', sa.String(50), nullable=False),
    )
    op.execute("INSERT INTO wp11_owner (id) VALUES (1)")
    op.execute("INSERT INTO wp11_account (id, owner_id, name) VALUES (1, 1, 'a')")"""

_REBUILD_OWNER = """\
    with op.batch_alter_table('wp11_owner', recreate='always') as batch:
        batch.add_column(sa.Column('label', sa.String(20)))"""


@pytest.mark.asyncio
async def test_a_batch_rebuild_of_a_parent_keeps_its_children(tmp_path: Path) -> None:
    """Rebuilding a parent drops it: with the application's enforced foreign keys the cascade would empty its
    children. The migration runs with them off, and checks them before it commits."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    ini = environment(tmp_path, [_PARENT_AND_CHILD, _REBUILD_OWNER])
    registry, engine = _registry_engine(url)
    try:
        await MigrationRunner(config_path=str(ini), engine=engine).start()
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM wp11_account"))).scalar() == 1
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar() == 1  # restored
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_a_migration_that_breaks_a_foreign_key_commits_nothing(tmp_path: Path) -> None:
    from pyfly.data.relational.migrations import MigrationError

    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    orphan = "    op.execute(\"INSERT INTO wp11_account (id, owner_id, name) VALUES (2, 99, 'orphan')\")"
    ini = environment(tmp_path, [_PARENT_AND_CHILD + "\n" + orphan])
    registry, engine = _registry_engine(url)
    try:
        with pytest.raises(MigrationError, match="foreign keys point nowhere"):
            await MigrationRunner(config_path=str(ini), engine=engine).start()
    finally:
        await registry.close()
    assert "wp11_account" not in await _tables(url)


_UNNAMED_CHECK = """\
    op.execute(
        "CREATE TABLE wp11_wallet (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, balance INTEGER NOT NULL, "
        "CHECK (balance >= 0))"
    )"""

_ALTER_WALLET = """\
    with op.batch_alter_table('wp11_wallet') as batch:
        batch.alter_column('name', existing_type=sa.String(50), nullable=True)"""


@pytest.mark.asyncio
async def test_a_batch_rebuild_that_would_drop_an_unnamed_check_fails(tmp_path: Path) -> None:
    """C095: Alembic cannot carry an unnamed CHECK over a SQLite rebuild. The rebuild fails, and the table keeps
    its rule."""
    from pyfly.data.relational.migrations import MigrationError

    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    registry, engine = _registry_engine(url)
    try:
        ini = environment(tmp_path, [_UNNAMED_CHECK])
        await MigrationRunner(config_path=str(ini), engine=engine).start()
        (tmp_path / "migrations" / "versions" / "wp11r002.py").write_text(_revision(2, _ALTER_WALLET, "wp11r001"))
        with pytest.raises(MigrationError, match=r"unnamed CHECK constraint\(s\) CHECK \(balance >= 0\)"):
            await MigrationRunner(config_path=str(ini), engine=engine).start()
        async with engine.connect() as connection:
            with pytest.raises(Exception, match="CHECK constraint failed"):
                await connection.execute(text("INSERT INTO wp11_wallet (id, name, balance) VALUES (1, 'x', -5)"))
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_a_batch_rebuild_keeps_a_named_check(tmp_path: Path) -> None:
    """The naming convention of Base names every CHECK: a named one is reflected and carried over."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    named = _UNNAMED_CHECK.replace("CHECK (balance >= 0)", "CONSTRAINT ck_wp11_wallet_balance CHECK (balance >= 0)")
    ini = environment(tmp_path, [named, _ALTER_WALLET])
    registry, engine = _registry_engine(url)
    try:
        await MigrationRunner(config_path=str(ini), engine=engine).start()
        async with engine.connect() as connection:
            with pytest.raises(Exception, match="CHECK constraint failed"):
                await connection.execute(text("INSERT INTO wp11_wallet (id, name, balance) VALUES (1, NULL, -5)"))
    finally:
        await registry.close()
