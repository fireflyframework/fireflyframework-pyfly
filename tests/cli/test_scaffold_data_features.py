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
"""Scaffolded projects with data features, generated and run as a developer gets them.

- C096: the hexagonal archetype with a data feature generated a DELETE that always failed
  (``repository.delete(<id>)``) and a test suite that could not import (``InMemoryTodoRepository``).
- C097: data-relational plus data-document emitted two ``data:`` keys in ``pyfly.yaml``; YAML kept the last
  one and the relational datasource silently disappeared.
- C098: scaffolded entities could not insert on Oracle (an integer key without identity, a NOT NULL column
  defaulting to ``''``, which Oracle stores as NULL).
- C163: the generated data tests ran on ``sqlite:///:memory:`` (one connection shared by every session, so
  transaction semantics are untestable); they are ``@DataTest`` classes on a SQLite file now.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pyfly.cli.templates import DEFAULT_FEATURES, generate_project

_DATA_FEATURES: dict[str, list[str]] = {
    "none": [],
    "relational": ["data-relational"],
    "document": ["data-document"],
    "both": ["data-relational", "data-document"],
}


def _generate(root: Path, archetype: str, data: str) -> Path:
    project = root / f"{archetype}-{data}"
    features = [*DEFAULT_FEATURES[archetype], *_DATA_FEATURES[data]]
    generate_project(name="shop", project_dir=project, archetype=archetype, features=features, package_name="shop")
    return project


def _run_suite(project: Path) -> subprocess.CompletedProcess[str]:
    """The generated project's own test suite, in a process of its own (its models only)."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(project / "src")},
    )


# The archetypes that generate a test suite (core and library generate none).
_WITH_TESTS = [name for name in DEFAULT_FEATURES if name not in ("core", "library")]


@pytest.mark.parametrize("data", list(_DATA_FEATURES))
@pytest.mark.parametrize("archetype", _WITH_TESTS)
def test_every_archetype_and_data_feature_generates_a_passing_suite(tmp_path: Path, archetype: str, data: str) -> None:
    project = _generate(tmp_path, archetype, data)
    result = _run_suite(project)
    assert result.returncode == 0, f"{archetype} with {data}:\n{result.stdout}\n{result.stderr}"
    assert " passed" in result.stdout


@pytest.mark.parametrize("archetype", ["web-api", "hexagonal"])
def test_the_relational_suite_is_a_data_test_on_a_file_database(tmp_path: Path, archetype: str) -> None:
    project = _generate(tmp_path, archetype, "relational")
    tests = "\n".join(path.read_text() for path in (project / "tests").rglob("test_*.py"))
    assert "@DataTest(beans=[" in tests
    assert ":memory:" not in tests
    assert "test_delete_todo" in tests  # the DELETE a request runs, through the real repository


@pytest.mark.parametrize("archetype", ["web-api", "hexagonal", "fastapi-api"])
def test_the_hexagonal_and_api_services_delete_by_id(tmp_path: Path, archetype: str) -> None:
    for data in ("relational", "document"):
        project = _generate(tmp_path, archetype, data)
        services = "\n".join(path.read_text() for path in (project / "src" / "shop").rglob("*service*.py"))
        assert ".delete_by_id(" in services, (archetype, data)
        assert "._repository.delete(" not in services, (archetype, data)


@pytest.mark.parametrize("archetype", [name for name in DEFAULT_FEATURES if name != "library"])
def test_both_data_features_keep_both_datasources(tmp_path: Path, archetype: str) -> None:
    project = _generate(tmp_path, archetype, "both")
    text = (project / "pyfly.yaml").read_text()
    assert text.count("\n  data:\n") == 1, text
    data = yaml.safe_load(text)["pyfly"]["data"]
    assert data["relational"]["enabled"] is True
    assert data["relational"]["url"] == "sqlite+aiosqlite:///shop.db"
    assert data["document"]["enabled"] is True
    assert data["document"]["database"] == "shop"


_COMPILE = """\
import json, sys
from sqlalchemy import insert
from sqlalchemy.dialects import mssql, oracle, postgresql
from sqlalchemy.schema import CreateTable
sys.path.insert(0, sys.argv[1])
module = __import__(sys.argv[2], fromlist=["*"])
table = getattr(module, sys.argv[3]).__table__
out = {}
for name, dialect in (("oracle", oracle.dialect()), ("mssql", mssql.dialect()), ("postgresql", postgresql.dialect())):
    out[name] = {
        "ddl": str(CreateTable(table).compile(dialect=dialect)),
        "insert": str(insert(table).values(title="t").compile(dialect=dialect)),
    }
out["nullable"] = {column.name: column.nullable for column in table.columns}
print(json.dumps(out))
"""


def _compiled(src: Path, module: str, entity: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", _COMPILE, str(src), module, entity], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("archetype", "module", "entity"),
    [("web-api", "shop.models.todo", "TodoEntity"), ("hexagonal", "shop.domain.models", "TodoEntity")],
)
def test_scaffolded_entities_insert_on_oracle_and_sql_server(
    tmp_path: Path, archetype: str, module: str, entity: str
) -> None:
    project = _generate(tmp_path, archetype, "relational")
    compiled = _compiled(project / "src", module, entity)
    oracle, mssql, postgresql = compiled["oracle"], compiled["mssql"], compiled["postgresql"]
    assert "id INTEGER GENERATED BY DEFAULT AS IDENTITY" in oracle["ddl"], oracle  # type: ignore[index]
    assert "id INTEGER NOT NULL IDENTITY" in mssql["ddl"], mssql  # type: ignore[index]
    assert "GENERATED BY DEFAULT AS IDENTITY" in postgresql["ddl"], postgresql  # type: ignore[index]
    # Oracle stores '' as NULL: an optional text column must accept NULL.
    assert compiled["nullable"] == {"id": False, "title": False, "description": True, "completed": False}


def test_a_generated_entity_has_an_identity_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``pyfly generate entity`` renders the same key."""
    from click.testing import CliRunner

    from pyfly.cli.main import cli

    project = _generate(tmp_path, "web-api", "relational")
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["generate", "entity", "invoice"])
    assert result.exit_code == 0, result.output
    (model,) = (project / "src" / "shop" / "models").glob("invoice*.py")
    text = model.read_text()
    assert "mapped_column(Identity(), primary_key=True)" in text
