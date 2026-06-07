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
"""Mapper extension tests (v26.06.24): Pydantic-awareness, nested-model and
collection recursion, and the declarative @mapping decorator."""

from __future__ import annotations

from pydantic import BaseModel

from pyfly.data import Mapper, default_mapper, mapping


# --- source / dest models (module-level so get_type_hints can resolve them) -
class AddressEntity(BaseModel):
    street: str
    city: str


class UserEntity(BaseModel):
    username: str
    address: AddressEntity
    tags: list[str] = []


class AddressDTO(BaseModel):
    street: str
    city: str


class UserDTO(BaseModel):
    username: str
    address: AddressDTO
    tags: list[str] = []


class TeamEntity(BaseModel):
    name: str
    members: list[UserEntity] = []


class TeamDTO(BaseModel):
    name: str
    members: list[UserDTO] = []


def test_nested_model_is_recursively_mapped() -> None:
    src = UserEntity(username="ada", address=AddressEntity(street="1 Main", city="London"), tags=["x"])
    dto = Mapper().map(src, UserDTO)
    assert isinstance(dto, UserDTO)
    assert isinstance(dto.address, AddressDTO)  # recursed, not left as AddressEntity / dict
    assert dto.address.city == "London"
    assert dto.username == "ada"
    assert dto.tags == ["x"]


def test_collection_of_models_is_recursively_mapped() -> None:
    team = TeamEntity(
        name="A",
        members=[UserEntity(username="ada", address=AddressEntity(street="s", city="c"))],
    )
    dto = Mapper().map(team, TeamDTO)
    assert isinstance(dto, TeamDTO)
    assert len(dto.members) == 1
    assert isinstance(dto.members[0], UserDTO)
    assert isinstance(dto.members[0].address, AddressDTO)  # nested-within-collection recursed


class _Src(BaseModel):
    username: str
    email: str


class _Dst(BaseModel):
    name: str
    email: str


def test_pydantic_rename_and_transform() -> None:
    m = Mapper()
    m.add_mapping(_Src, _Dst, field_map={"username": "name"}, transformers={"email": str.lower})
    d = m.map(_Src(username="Ada", email="A@B.COM"), _Dst)
    assert d.name == "Ada"
    assert d.email == "a@b.com"


@mapping(_Src, _Dst, rename={"username": "name"}, transform={"email": str.lower})
class _SrcToDstMapper:
    pass


def test_declarative_mapping_decorator() -> None:
    d = default_mapper.map(_Src(username="Bob", email="X@Y.Z"), _Dst)
    assert d.name == "Bob"
    assert d.email == "x@y.z"
    assert _SrcToDstMapper.__pyfly_mapping__ == (_Src, _Dst)
