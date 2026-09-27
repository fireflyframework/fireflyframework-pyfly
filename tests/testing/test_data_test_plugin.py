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
"""``@DataTest`` through PyFly's pytest plugin, in a pytest run of its own (C156, C177).

``@DataTest`` only set an attribute nothing read, while the docs presented it as a working data slice. With
the plugin, every test of a ``@DataTest`` class gets a started data slice (``data_context``) whose units of
work roll back when the test ends: a test that writes a unique row, then a test that writes it again on the
same database file, both pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pyfly.testing import DataTest, get_test_slice

_CONFTEST = """\
import pytest

from pyfly.core.config import Config


@pytest.fixture(scope="session")
def shared_database(tmp_path_factory):
    return tmp_path_factory.mktemp("db") / "shared.db"


@pytest.fixture
def pyfly_data_config(shared_database):
    # Every test on one database file: only the rollback keeps them apart.
    url = f"sqlite+aiosqlite:///{shared_database}"
    return Config({"pyfly": {"data": {"relational": {"enabled": "true", "url": url}}}})
"""

_TESTS = """\
import pytest
from sqlalchemy import Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.stereotypes import repository, service
from pyfly.data.relational.sqlalchemy import Base, Repository
from pyfly.data.relational.sqlalchemy.session import SessionProvider
from pyfly.data.transaction import transactional
from pyfly.testing import DataTest


class Member(Base):
    __tablename__ = "plugin_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(80), unique=True)


@repository
class MemberRepository(Repository[Member, int]):
    async def find_by_email(self, email: str) -> Member | None: ...


@service
class MemberService:
    def __init__(self, members: MemberRepository) -> None:
        self._members = members

    @transactional
    async def join(self, email: str) -> None:
        await self._members.save(Member(email=email))


@DataTest(beans=[MemberRepository, MemberService])
class TestMembers:
    async def test_first_writes_the_row(self, data_context) -> None:
        await data_context.get_bean(MemberService).join("same@example.com")
        members = data_context.get_bean(MemberRepository)
        assert (await members.find_by_email("same@example.com")) is not None

    async def test_second_writes_it_again(self, data_context) -> None:
        members = data_context.get_bean(MemberRepository)
        assert await members.count() == 0
        await data_context.get_bean(MemberService).join("same@example.com")

    async def test_a_test_that_does_not_ask_for_the_context_still_rolls_back(self) -> None:
        pass


@DataTest
class TestBare:
    async def test_units_of_the_session_provider_roll_back(self, data_context) -> None:
        async with data_context.get_bean(SessionProvider).unit() as session:
            await session.execute(text("INSERT INTO plugin_members (email) VALUES ('bare@example.com')"))
        async with data_context.get_bean(SessionProvider).unit(read_only=True) as session:
            count = (await session.execute(text("SELECT count(*) FROM plugin_members"))).scalar()
        assert count == 1


@pytest.mark.data_test(beans=[MemberRepository])
async def test_a_marked_function(data_context) -> None:
    members = data_context.get_bean(MemberRepository)
    assert await members.count() == 0
    await members.save(Member(email="same@example.com"))


@DataTest(beans=[MemberRepository], rollback=False)
class TestWithoutRollback:
    async def test_commits(self, data_context) -> None:
        await data_context.get_bean(MemberRepository).save(Member(email="kept@example.com"))

    async def test_sees_the_commit(self, data_context) -> None:
        assert await data_context.get_bean(MemberRepository).count() == 1
"""


def _run_pytest(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider", str(directory)],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def test_data_test_classes_run_in_a_slice_whose_units_roll_back(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_members.py").write_text(_TESTS)
    result = _run_pytest(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "7 passed" in result.stdout, result.stdout


def test_the_default_configuration_is_a_sqlite_file_per_test(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "test_default.py").write_text(_TESTS.split("@DataTest(beans=[MemberRepository], rollback=False)")[0])
    result = _run_pytest(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout, result.stdout


def test_data_test_marks_the_class_bare_or_with_options() -> None:
    @DataTest
    class Bare:
        pass

    @DataTest(beans=[int], rollback=False)
    class WithOptions:
        pass

    from pyfly.testing.slices import data_test_options

    assert get_test_slice(Bare) == get_test_slice(WithOptions) == "data"
    bare, with_options = data_test_options(Bare), data_test_options(WithOptions)
    assert bare is not None and bare.beans == () and bare.rollback is True
    assert with_options is not None and with_options.beans == (int,) and with_options.rollback is False
    assert [mark.args for mark in WithOptions.pytestmark] == [("data_context",)]  # type: ignore[attr-defined]


def test_the_marker_takes_keyword_arguments_only(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "test_positional.py").write_text(
        'import pytest\n\n\n@pytest.mark.data_test("beans")\nasync def test_x(data_context) -> None:\n    pass\n'
    )
    result = _run_pytest(tmp_path)
    assert result.returncode != 0
    assert "keyword arguments only" in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["data_context", "pyfly_data_config"])
def test_the_plugin_is_registered_with_pytest(pytestconfig: pytest.Config, name: str) -> None:
    assert pytestconfig.pluginmanager.has_plugin("pyfly")
    plugin = pytestconfig.pluginmanager.get_plugin("pyfly")
    assert hasattr(plugin, name)
