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
"""The backend matrix's plumbing, without Docker: lane parametrization and selection, the
per-lane configuration, and the dependency floors the MySQL/MariaDB lanes rely on (C161, C089).

Collection is checked in a separate pytest process on a generated test module, so the real marker
expressions (``-m integration``, ``-m "integration and mysql"``) are what decides the selection.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import sqlalchemy
from packaging.requirements import Requirement
from packaging.version import Version

from tests.support.backend_matrix import (
    MARIADB,
    MYSQL,
    PG,
    RELATIONAL_LANES,
    SQLITE_FILE,
    MongoBackend,
    RelationalBackend,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRE_PING_FIXED = Version("2.0.50")  # SQLAlchemy release that fixed the MySQL async ping adapters (C089)

_GENERATED = """
import pytest

def test_every_lane(relational_backend):
    pass

@pytest.mark.backends("sqlite-file", "pg")
def test_two_lanes(relational_backend):
    pass

def test_replica_set(mongo_rs_url):
    pass

def test_no_backend():
    pass
"""


def _collect(tmp_path: Path, *args: str, source: str = _GENERATED) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_generated.py").write_text(source)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(_REPO_ROOT), os.environ.get("PYTHONPATH")]))}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.support.backend_matrix", "-p", "no:cacheprovider"]
        + ["--collect-only", "-q", *args, "test_generated.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def _ids(result: subprocess.CompletedProcess[str]) -> list[str]:
    assert result.returncode in (0, 5), result.stdout + result.stderr  # 5: everything deselected
    return [line.split("::", 1)[1] for line in result.stdout.splitlines() if line.startswith("test_generated.py::")]


def test_relational_backend_runs_once_per_lane(tmp_path: Path) -> None:
    assert _ids(_collect(tmp_path)) == [
        "test_every_lane[sqlite-file]",
        "test_every_lane[pg]",
        "test_every_lane[mysql]",
        "test_every_lane[mariadb]",
        "test_two_lanes[sqlite-file]",
        "test_two_lanes[pg]",
        "test_replica_set",
        "test_no_backend",
    ]


def test_the_fast_suite_keeps_only_the_sqlite_file_lane(tmp_path: Path) -> None:
    assert _ids(_collect(tmp_path, "-m", "not integration")) == [
        "test_every_lane[sqlite-file]",
        "test_two_lanes[sqlite-file]",
        "test_no_backend",
    ]


def test_the_integration_suite_runs_every_server_lane(tmp_path: Path) -> None:
    assert _ids(_collect(tmp_path, "-m", "integration")) == [
        "test_every_lane[pg]",
        "test_every_lane[mysql]",
        "test_every_lane[mariadb]",
        "test_two_lanes[pg]",
        "test_replica_set",
    ]


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("pg", ["test_every_lane[pg]", "test_two_lanes[pg]"]),
        ("mysql", ["test_every_lane[mysql]"]),
        ("mariadb", ["test_every_lane[mariadb]"]),
        ("mongo", ["test_replica_set"]),
    ],
)
def test_one_lane_is_selected_by_its_marker(tmp_path: Path, marker: str, expected: list[str]) -> None:
    assert _ids(_collect(tmp_path, "-m", f"integration and {marker}")) == expected


_TREE = {
    "tests/integration/conftest.py": (
        "import pytest\n\n\n@pytest.fixture\ndef pg_url():\n    return 'postgresql+asyncpg://u:p@h:5432/db'\n"
    ),
    "tests/integration/test_server.py": "def test_on_pg(pg_url):\n    pass\n",
    "tests/unit/test_fakes.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef redis_url():\n    return 'redis://fake'\n\n\n"
        "def test_local_fake(redis_url):\n    pass\n\n\n"
        "@pytest.mark.parametrize('mongo_url', ['mongodb://fake'])\ndef test_direct_param(mongo_url):\n    pass\n\n\n"
        "def test_plugin_server(mongo_rs_url):\n    pass\n"
    ),
}


def _collect_tree(tmp_path: Path, *args: str) -> list[str]:
    for relative, source in _TREE.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(source)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(_REPO_ROOT), os.environ.get("PYTHONPATH")]))}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.support.backend_matrix", "-p", "no:cacheprovider"]
        + ["--collect-only", "-q", *args, "tests"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if line.startswith("tests/")]


def test_a_same_named_fixture_outside_the_server_fixtures_stays_in_the_fast_suite(tmp_path: Path) -> None:
    assert _collect_tree(tmp_path, "-m", "not integration") == [
        "tests/unit/test_fakes.py::test_local_fake",
        "tests/unit/test_fakes.py::test_direct_param[mongodb://fake]",
    ]


def test_the_server_fixtures_of_the_plugin_and_of_tests_integration_mark_their_lane(tmp_path: Path) -> None:
    assert _collect_tree(tmp_path, "-m", "integration and pg") == ["tests/integration/test_server.py::test_on_pg"]
    assert _collect_tree(tmp_path, "-m", "integration and mongo") == ["tests/unit/test_fakes.py::test_plugin_server"]


def test_an_unknown_lane_is_a_usage_error(tmp_path: Path) -> None:
    source = "import pytest\n\n@pytest.mark.backends('oracle')\ndef test_x(relational_backend):\n    pass\n"
    result = _collect(tmp_path, source=source)
    assert result.returncode != 0
    assert "@pytest.mark.backends takes lanes from" in result.stdout + result.stderr


def test_lanes_are_listed_in_matrix_order() -> None:
    assert RELATIONAL_LANES == (SQLITE_FILE, PG, MYSQL, MARIADB)


@pytest.mark.parametrize(
    ("lane", "url", "pre_ping"),
    [
        (SQLITE_FILE, "sqlite+aiosqlite:////tmp/x/pyfly.db", False),
        (PG, "postgresql+asyncpg://u:p@h:5432/pyfly_t_1", False),
        (MYSQL, "mysql+asyncmy://root:pyfly@h:3306/pyfly_t_1", True),
        (MARIADB, "mariadb+asyncmy://root:pyfly@h:3306/pyfly_t_1", True),
    ],
)
def test_relational_backend_config_per_lane(lane: str, url: str, pre_ping: bool) -> None:
    backend = RelationalBackend(lane, url)
    config = backend.config({"pyfly.data.relational.ddl-auto": "create"})

    assert backend.pre_ping is pre_ping
    assert backend.engine_options() == ({"pool_pre_ping": True} if pre_ping else {})
    assert config.get("pyfly.data.relational.enabled") == "true"
    assert config.get("pyfly.data.relational.url") == url
    assert config.get("pyfly.data.relational.ddl-auto") == "create"  # overrides win
    assert config.get("pyfly.data.relational.pool.pre-ping") == ("true" if pre_ping else None)
    assert RelationalBackend(lane, url).config().get("pyfly.data.relational.ddl-auto") == "none"


def test_with_driver_keeps_the_database() -> None:
    backend = RelationalBackend(MARIADB, "mariadb+asyncmy://root:pyfly@h:3306/pyfly_t_1")
    other = backend.with_driver("aiomysql")
    assert other.url == "mariadb+aiomysql://root:pyfly@h:3306/pyfly_t_1"
    assert (other.lane, other.dialect, other.driver) == (MARIADB, "mariadb", "aiomysql")


def test_mongo_backend_config() -> None:
    config = MongoBackend("mongodb://h:1/?directConnection=true", "pyfly_t_1").config()
    assert config.get("pyfly.data.document.enabled") == "true"
    assert config.get("pyfly.data.document.uri") == "mongodb://h:1/?directConnection=true"
    assert config.get("pyfly.data.document.database") == "pyfly_t_1"


# ---------------------------------------------------------------------------
# C089: the dependency floor and the MySQL extra
# ---------------------------------------------------------------------------


def _pyproject() -> dict[str, object]:
    return tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())


def _requirement(requirements: list[str], name: str) -> Requirement:
    return next(req for req in map(Requirement, requirements) if req.name == name)


def test_data_relational_requires_a_sqlalchemy_whose_mysql_pre_ping_works() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    floor = _requirement(extras["data-relational"], "sqlalchemy")
    assert not floor.specifier.contains("2.0.49"), f"{floor} still admits 2.0.49, whose pre-ping breaks MySQL"
    assert floor.specifier.contains(str(_PRE_PING_FIXED))

    locked = tomllib.loads((_REPO_ROOT / "uv.lock").read_text())["package"]
    locked_version = Version(next(pkg["version"] for pkg in locked if pkg["name"] == "sqlalchemy"))
    assert locked_version >= _PRE_PING_FIXED
    assert Version(sqlalchemy.__version__) >= _PRE_PING_FIXED


def test_the_mysql_extra_installs_asyncmy() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    assert _requirement(extras["mysql"], "asyncmy")
    assert any("mysql" in Requirement(req).extras for req in extras["full"])
