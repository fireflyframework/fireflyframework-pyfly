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
"""``pyfly db`` in a project, as a developer runs it (a process per command), on every backend of the matrix.

- C026: the ``env.py`` of ``pyfly db init`` imported no model, so ``pyfly db migrate`` on a database with
  tables wrote a revision that dropped every one of them (and startup migrations applied it).
- C027: the framework's tables were outside ``target_metadata``: autogenerate dropped them. Now a migration
  creates them, and the stores that only check their tables (``ddl-auto`` none) find them.
- C119: the commands used ``alembic.ini``'s placeholder URL instead of the application's datasource.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.support.backend_matrix import RelationalBackend

_MODELS = """\
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class Customer(Base):
    __tablename__ = "wp11_cli_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Order(Base):
    __tablename__ = "wp11_cli_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("wp11_cli_customers.id"))
"""

_INVOICE_COLUMN = "    invoice: Mapped[str | None] = mapped_column(String(40), default=None)\n"


def pyfly(
    project: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """``pyfly <args>`` in *project*, in a process of its own: ``Base.metadata`` holds the project's models
    only, as for a developer."""
    import os

    result = subprocess.run(
        [sys.executable, "-c", "from pyfly.cli.main import cli; cli()", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if check:
        assert result.returncode == 0, f"pyfly {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    return result


def make_project(root: Path, url: str, *, models: str | None = _MODELS, extra: dict[str, object] | None = None) -> Path:
    """A src-layout project ``shop`` whose ``pyfly.yaml`` names *url* as the primary datasource."""
    project = root / "shop-project"
    package = project / "src" / "shop"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "shop"\nversion = "0.1.0"\n')
    (package / "__init__.py").write_text("")
    if models is not None:
        (package / "models.py").write_text(models)
    relational: dict[str, object] = {"enabled": True, "url": url}
    if url.startswith(("mysql", "mariadb")):
        relational["pool"] = {"pre-ping": True}
    relational.update(extra or {})
    (project / "pyfly.yaml").write_text(yaml.safe_dump({"pyfly": {"data": {"relational": relational}}}))
    return project


def upgrade_body(revision: Path) -> str:
    return revision.read_text().split("def upgrade")[1].split("def downgrade")[0]


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


async def test_a_project_migrates_its_models_and_the_framework_tables(
    relational_backend: RelationalBackend, tmp_path: Path
) -> None:
    from pyfly.data.relational.framework_schema import cache_entries, ensure_tables, locks, orchestration_state, users

    url = relational_backend.url
    project = make_project(tmp_path, url)
    versions = project / "alembic" / "versions"
    await asyncio.to_thread(pyfly, project, "db", "init")
    assert "driver://user:pass@localhost/dbname" in (project / "alembic.ini").read_text()  # untouched

    await asyncio.to_thread(pyfly, project, "db", "migrate", "-m", "initial")
    (initial,) = versions.glob("*_initial.py")
    body = upgrade_body(initial)
    for table in ("wp11_cli_customers", "wp11_cli_orders", "pyfly_locks", "pyfly_orchestration_state"):
        assert f"op.create_table('{table}'" in body, body
    assert "import pyfly.data.relational.framework_schema" in initial.read_text()

    await asyncio.to_thread(pyfly, project, "db", "upgrade")
    assert {"wp11_cli_customers", "wp11_cli_orders", "pyfly_locks", "alembic_version"} <= await _tables(url)
    # The stores that only check their tables (ddl-auto none) find what the migration created.
    engine = relational_backend.create_engine()
    await ensure_tables(engine, orchestration_state, cache_entries, locks, users, create=False)

    # A model change: the revision adds the column and drops nothing.
    models = project / "src" / "shop" / "models.py"
    models.write_text(
        models.read_text().replace(
            "    name: Mapped[str] = mapped_column(String(50))\n",
            "    name: Mapped[str] = mapped_column(String(50))\n" + _INVOICE_COLUMN,
        )
    )
    await asyncio.to_thread(pyfly, project, "db", "migrate", "-m", "invoice")
    (invoice,) = versions.glob("*_invoice.py")
    body = upgrade_body(invoice)
    assert "add_column(sa.Column('invoice'" in body, body
    assert "drop_table" not in body, body
    await asyncio.to_thread(pyfly, project, "db", "upgrade")

    await asyncio.to_thread(pyfly, project, "db", "migrate", "-m", "unchanged")
    (unchanged,) = versions.glob("*_unchanged.py")
    assert "op." not in upgrade_body(unchanged), unchanged.read_text()


@pytest.mark.backends("sqlite-file")
async def test_autogenerate_without_models_refuses_to_drop_the_tables(
    relational_backend: RelationalBackend, tmp_path: Path
) -> None:
    """C026: env.py imports no model (nothing is configured, and the project has no package): the revision
    would drop the database's tables, so no revision is written."""
    url = relational_backend.url
    project = make_project(tmp_path, url, models=None)
    (project / "pyproject.toml").unlink()
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))
    await asyncio.to_thread(pyfly, project, "db", "init")

    result = await asyncio.to_thread(pyfly, project, "db", "migrate", "-m", "oops", check=False)
    assert result.returncode == 1
    assert "found no entity model" in result.stdout + result.stderr
    assert list((project / "alembic" / "versions").glob("*.py")) == []


@pytest.mark.backends("sqlite-file")
async def test_the_commands_follow_the_active_profile(relational_backend: RelationalBackend, tmp_path: Path) -> None:
    """The application's configuration is loaded as the application loads it: the dev profile's URL wins."""
    project = make_project(tmp_path, relational_backend.url, extra={"migrations": {"models": ["shop.models"]}})
    dev_url = f"sqlite+aiosqlite:///{tmp_path / 'dev.db'}"
    (project / "pyfly-dev.yaml").write_text(yaml.safe_dump({"pyfly": {"data": {"relational": {"url": dev_url}}}}))
    await asyncio.to_thread(pyfly, project, "db", "init")
    env = {"PYFLY_PROFILES_ACTIVE": "dev"}
    await asyncio.to_thread(pyfly, project, "db", "migrate", "-m", "initial", env=env)
    await asyncio.to_thread(pyfly, project, "db", "upgrade", env=env)
    assert "wp11_cli_orders" in await _tables(dev_url)
    assert "wp11_cli_orders" not in await _tables(relational_backend.url)
