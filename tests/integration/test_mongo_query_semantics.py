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
"""MongoDB queries mean what they mean on SQL, on a real server.

- C036: ``like`` is anchored and case-sensitive, ``containing``/``starting_with``/``ending_with`` match their
  argument as it is, ``_ignore_case`` ignores case, and the derived compiler and the filter operators agree;
  a prefix pattern can use an index.
- C110: ``!=``, ``not_in``, ``not_like`` and ``~spec`` are false for a null or missing field, as on SQL.
- WP04's parser: ``and`` binds tighter than ``or``; projections are read with a server-side projection;
  single results, pages and slices follow the return annotation.

The expected rows are those PostgreSQL returns for the same data and the same query.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

import pytest

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.filter import MongoFilterOperator as F
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.specification import MongoSpecification
from pyfly.data.page import Page, Slice
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.projection import projection
from pyfly.data.query_parser import IncorrectResultSizeException
from tests.support.mongo import BeanieDatabase, beanie_database


class QsPerson(BaseDocument):
    name: str
    role: str | None = None
    code: str = ""
    age: int = 0

    class Settings:
        name = "qs_people"
        indexes = ["code"]


@projection
class PersonName(Protocol):
    name: str


class PeopleRepository(MongoRepository[QsPerson, str]):
    async def find_by_name_like(self, pattern: str) -> list[QsPerson]: ...

    async def find_by_name_containing(self, fragment: str) -> list[QsPerson]: ...

    async def find_by_name_containing_ignore_case(self, fragment: str) -> list[QsPerson]: ...

    async def find_by_name_starting_with(self, prefix: str) -> list[QsPerson]: ...

    async def find_by_name_ending_with(self, suffix: str) -> list[QsPerson]: ...

    async def find_by_code_like(self, pattern: str) -> list[QsPerson]: ...

    async def find_by_name_ignore_case(self, name: str) -> list[QsPerson]: ...

    async def find_by_role_not(self, role: str) -> list[QsPerson]: ...

    async def find_by_role_not_in(self, roles: list[str]) -> list[QsPerson]: ...

    async def find_by_role_not_like(self, pattern: str) -> list[QsPerson]: ...

    async def count_by_role_not(self, role: str) -> int: ...

    async def find_by_role_or_name_and_age(self, role: str, name: str, age: int) -> list[QsPerson]: ...

    async def find_by_age_greater_than(self, age: int) -> list[PersonName]: ...

    async def find_by_role(self, role: str) -> QsPerson | None: ...

    async def find_by_age_less_than(self, age: int, pageable: Pageable) -> Page[QsPerson]: ...

    async def find_by_age_greater_than_equal(self, age: int, pageable: Pageable) -> Slice[QsPerson]: ...

    async def find_by_code_in(self, codes: list[str], sort: Sort) -> list[QsPerson]: ...


PEOPLE = [
    ("Al", "admin", "INV-001", 30),
    ("Alice", "user", "XINV-002", 25),
    ("Sal", None, "A-01", 41),
    ("alice", "admin", "A-012", 35),
    ("xAlpha", None, "B%1", 20),
]


@pytest.fixture
async def people(mongo_rs_url: str) -> AsyncIterator[tuple[PeopleRepository, BeanieDatabase]]:
    async with beanie_database(mongo_rs_url, [QsPerson]) as database:
        repository = PeopleRepository()
        MongoRepositoryBeanPostProcessor().after_init(repository, "people")
        await repository.save_all([QsPerson(name=n, role=r, code=c, age=a) for n, r, c, a in PEOPLE])
        # A document stored without the field at all (older data): missing is null.
        await database.database["qs_people"].insert_one({"name": "Missing", "code": "M", "age": 50})
        database.log.clear()
        yield repository, database


def names(people: list[QsPerson]) -> list[str]:
    return sorted(person.name for person in people)


async def spec_names(repository: PeopleRepository, spec: MongoSpecification[QsPerson]) -> list[str]:
    return names(await repository.find_all_by_spec(spec))


# ---------------------------------------------------------------------------------------------------------
# C036: pattern matching
# ---------------------------------------------------------------------------------------------------------


async def test_like_is_anchored_and_case_sensitive(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, _db = people
    assert names(await repository.find_by_name_like("Al%")) == ["Al", "Alice"]
    assert names(await repository.find_by_code_like("INV-%")) == ["Al"]  # not XINV-002
    assert names(await repository.find_by_code_like("%-01")) == ["Sal"]
    assert names(await repository.find_by_code_like("B%1")) == ["xAlpha"]  # a pattern's % is a wildcard
    assert await spec_names(repository, F.like("name", "Al%")) == ["Al", "Alice"]
    assert await spec_names(repository, F.like("name", "al%", ignore_case=True)) == ["Al", "Alice", "alice"]


async def test_containing_and_its_siblings_match_the_argument_as_it_is(
    people: tuple[PeopleRepository, BeanieDatabase],
) -> None:
    repository, _db = people
    assert names(await repository.find_by_name_containing("al")) == ["Sal", "alice"]
    everyone_but_missing = ["Al", "Alice", "Sal", "alice", "xAlpha"]
    assert names(await repository.find_by_name_containing_ignore_case("al")) == everyone_but_missing
    assert names(await repository.find_by_name_starting_with("Al")) == ["Al", "Alice"]
    assert names(await repository.find_by_name_ending_with("ice")) == ["Alice", "alice"]
    assert await spec_names(repository, F.contains("name", "al")) == ["Sal", "alice"]
    assert names(await repository.find_by_code_like("%\\%%")) == []  # LIKE has no escape character here
    assert await spec_names(repository, F.contains("code", "%")) == ["xAlpha"]  # contains takes % literally
    assert names(await repository.find_by_name_ignore_case("ALICE")) == ["Alice", "alice"]


async def test_a_prefix_pattern_uses_the_index(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, db = people
    await repository.find_by_code_like("INV-%")
    ((_name, command),) = db.log.commands
    explained = await db.database.command({"explain": {"find": "qs_people", "filter": command["filter"]}})
    plan = str(explained["queryPlanner"]["winningPlan"])
    # An anchored prefix scans the keys from "INV-" on; an unanchored pattern would scan them all ('["", {})').
    assert "IXSCAN" in plan and "code_1" in plan
    assert '["INV-", "INV.")' in plan and '["", {})' not in plan


# ---------------------------------------------------------------------------------------------------------
# C110: negation
# ---------------------------------------------------------------------------------------------------------


async def test_not_equal_is_false_for_null_and_missing_fields(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, _db = people
    assert names(await repository.find_by_role_not("admin")) == ["Alice"]
    assert await repository.count_by_role_not("admin") == 1
    assert await spec_names(repository, F.neq("role", "admin")) == ["Alice"]
    assert await spec_names(repository, ~F.eq("role", "admin")) == ["Alice"]


async def test_not_in_and_not_like_are_false_for_null(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, _db = people
    assert names(await repository.find_by_role_not_in(["admin"])) == ["Alice"]
    assert names(await repository.find_by_role_not_like("adm%")) == ["Alice"]
    assert await spec_names(repository, ~F.in_list("role", ["user"])) == ["Al", "alice"]
    assert await spec_names(repository, ~F.like("role", "us%")) == ["Al", "alice"]


async def test_negating_a_composite_specification_follows_sql(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, _db = people
    # NOT (role = 'admin' OR age > 30) holds only where both are false: Alice (user, 25). For xAlpha (no role,
    # 20) the OR is unknown (NULL OR FALSE), and so is its negation: SQL leaves the row out.
    either = F.eq("role", "admin") | F.gt("age", 30)
    assert await spec_names(repository, ~either) == ["Alice"]
    # NOT (role = 'admin' AND age > 30): Al (admin, 30) and Alice (user) are false; Sal/xAlpha have no role:
    # role = 'admin' is unknown there, and age > 30 decides only when it is false (xAlpha, 20).
    both = F.eq("role", "admin") & F.gt("age", 30)
    assert await spec_names(repository, ~both) == ["Al", "Alice", "xAlpha"]
    assert await spec_names(repository, ~~F.eq("role", "admin")) == ["Al", "alice"]
    assert await spec_names(repository, F.is_null("role")) == ["Missing", "Sal", "xAlpha"]
    assert await spec_names(repository, ~F.is_null("role")) == ["Al", "Alice", "alice"]


# ---------------------------------------------------------------------------------------------------------
# WP04's parser and result shapes
# ---------------------------------------------------------------------------------------------------------


async def test_and_binds_tighter_than_or(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, _db = people
    # role = 'user' OR (name = 'alice' AND age = 35); read left to right, (role = 'user' OR name = 'alice') AND
    # age = 35 would find only 'alice' here, and nobody below.
    assert names(await repository.find_by_role_or_name_and_age("user", "alice", 35)) == ["Alice", "alice"]
    assert names(await repository.find_by_role_or_name_and_age("user", "alice", 99)) == ["Alice"]


async def test_a_projection_is_read_with_a_server_side_projection(
    people: tuple[PeopleRepository, BeanieDatabase],
) -> None:
    repository, db = people
    found = await repository.find_by_age_greater_than(34)
    assert sorted(person.name for person in found) == ["Missing", "Sal", "alice"]
    ((_name, command),) = db.log.commands
    assert command["projection"] == {"name": 1, "_id": 0}


async def test_single_results_pages_slices_and_sorts(people: tuple[PeopleRepository, BeanieDatabase]) -> None:
    repository, _db = people
    user = await repository.find_by_role("user")
    assert user is not None and user.name == "Alice"
    assert await repository.find_by_role("nobody") is None
    with pytest.raises(IncorrectResultSizeException):
        await repository.find_by_role("admin")
    page = await repository.find_by_age_less_than(40, Pageable.of(1, 2, Sort.by("age")))
    assert [person.name for person in page.items] == ["xAlpha", "Alice"] and page.total == 4
    window = await repository.find_by_age_greater_than_equal(30, Pageable.of(1, 3, Sort.by("age")))
    assert [person.name for person in window.items] == ["Al", "alice", "Sal"] and window.has_next is True
    ordered = await repository.find_by_code_in(["A-01", "INV-001", "M"], Sort.by("code"))
    assert [person.name for person in ordered] == ["Sal", "Al", "Missing"]
