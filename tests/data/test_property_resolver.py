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
"""``PropertyResolver`` validates sort, filter and example property names against the entity (WP03-16, C141).

Sort and filter names used to reach ``getattr(model, name)``: a relationship, a Python ``@property`` or a typo
gave a 500, a hidden column (``password_hash``) was sortable and filterable (an equality oracle), and a Mongo
operator key (``$where``) went straight to the server. The resolver accepts only the entity's mapped
properties (optionally narrowed by an allow-list), rejects anything else with a typed error the web layer
maps to 400, and maps a name to the backend's own (a pydantic alias, Mongo's ``_id``).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pyfly.data.pageable import NullHandling, Order, Sort
from pyfly.data.property_resolver import (
    InvalidPropertyError,
    PropertyResolver,
    register_property_introspector,
    unregister_property_introspector,
)
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.kernel.exceptions import InvalidRequestException


class PrAccount(Base):
    __tablename__ = "pr_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(128))
    tags: Mapped[list[PrTag]] = relationship(back_populates="account")

    @property
    def display_name(self) -> str:
        return self.name.title()


class PrTag(Base):
    __tablename__ = "pr_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("pr_account.id"))
    account: Mapped[PrAccount] = relationship(back_populates="tags")


class PrDocument(BaseModel):
    title: str
    created_by: str = Field(alias="createdBy")


class TestSqlAlchemyEntity:
    def test_mapped_columns_resolve_to_themselves(self) -> None:
        resolver = PropertyResolver.for_entity(PrAccount)
        assert resolver.resolve("name") == "name"
        assert set(resolver.properties) == {"id", "name", "password_hash"}

    @pytest.mark.parametrize("name", ["tags", "display_name", "nmae", "__table__", "__init__", "metadata", ""])
    def test_anything_but_a_mapped_column_is_refused(self, name: str) -> None:
        with pytest.raises(InvalidPropertyError) as caught:
            PropertyResolver.for_entity(PrAccount).resolve(name, usage="sort")
        error = caught.value
        assert isinstance(error, InvalidRequestException)  # the web layer answers 400
        assert isinstance(error, ValueError)
        assert (error.entity, error.property, error.usage) == ("PrAccount", name, "sort")
        assert "PrAccount" in str(error) and "id, name, password_hash" in str(error)

    def test_an_operator_key_is_refused_whatever_the_entity_has(self) -> None:
        resolver = PropertyResolver(PrAccount, {"$where": "$where", "name": "name"})
        with pytest.raises(InvalidPropertyError, match=r"\$"):
            resolver.resolve("$where", usage="filter")

    def test_an_allow_list_hides_the_other_columns(self) -> None:
        resolver = PropertyResolver.for_entity(PrAccount, allowed=("id", "name"))
        assert resolver.resolve("name") == "name"
        with pytest.raises(InvalidPropertyError, match="password_hash"):
            resolver.resolve("password_hash", usage="filter")
        assert set(resolver.properties) == {"id", "name"}

    def test_an_allow_list_naming_an_unknown_property_fails_at_once(self) -> None:
        with pytest.raises(ValueError, match="nmae"):
            PropertyResolver.for_entity(PrAccount, allowed=("nmae",))

    def test_a_sort_is_validated_and_keeps_its_orders(self) -> None:
        resolver = PropertyResolver.for_entity(PrAccount)
        sort = Sort.by(Order.desc("name").nulls_last().ignoring_case(), "id")
        assert resolver.resolve_sort(sort) == sort
        with pytest.raises(InvalidPropertyError):
            resolver.resolve_sort(Sort.by("tags"))

    def test_resolve_all_reports_the_first_bad_name(self) -> None:
        with pytest.raises(InvalidPropertyError, match="'nope'"):
            PropertyResolver.for_entity(PrAccount).resolve_all(["name", "nope"], usage="filter")


class TestPydanticModel:
    def test_fields_resolve_to_their_alias(self) -> None:
        resolver = PropertyResolver.for_entity(PrDocument)
        assert resolver.resolve("title") == "title"
        assert resolver.resolve("created_by") == "createdBy"
        sort = resolver.resolve_sort(Sort.by(Order.desc("created_by").nulls_first()))
        assert sort.orders == (Order("createdBy", "desc", NullHandling.NULLS_FIRST),)


class Opaque:
    pass


@pytest.fixture
def opaque_introspector() -> Iterator[None]:
    def introspect(entity: type) -> Mapping[str, str] | None:
        return {"id": "_id", "label": "label"} if entity is Opaque else None

    register_property_introspector(introspect)
    try:
        yield
    finally:
        unregister_property_introspector(introspect)


def test_a_registered_introspector_describes_other_entity_kinds(opaque_introspector: None) -> None:
    resolver = PropertyResolver.for_entity(Opaque)
    assert resolver.resolve("id") == "_id"
    with pytest.raises(InvalidPropertyError):
        resolver.resolve("_id")


def test_an_entity_nobody_can_describe_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="Opaque"):
        PropertyResolver.for_entity(Opaque)
