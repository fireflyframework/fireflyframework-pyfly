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
"""Tests for Mapper — generic type-to-type mapping (MapStruct-like)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.data.mapper import Mapper

# ---------------------------------------------------------------------------
# Test types
# ---------------------------------------------------------------------------


@dataclass
class UserEntity:
    id: int
    username: str
    email: str
    active: bool = True


@dataclass
class UserDTO:
    username: str
    email: str


@dataclass
class UserResponse:
    name: str
    email: str
    is_active: bool = True


@dataclass
class ProfileDTO:
    username: str
    email: str
    bio: str = ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBasicAutoMapping:
    """Auto-mapping between dataclasses with matching field names."""

    def test_matching_fields_are_mapped(self) -> None:
        entity = UserEntity(id=1, username="alice", email="alice@example.com")
        mapper = Mapper()

        dto = mapper.map(entity, UserDTO)

        assert isinstance(dto, UserDTO)
        assert dto.username == "alice"
        assert dto.email == "alice@example.com"


class TestEntityToDTO:
    """Mapping from an entity-like object to a plain DTO."""

    def test_entity_extra_fields_are_ignored(self) -> None:
        """Source has more fields than dest — extras are silently dropped."""
        entity = UserEntity(id=42, username="bob", email="bob@test.com", active=False)
        mapper = Mapper()

        dto = mapper.map(entity, UserDTO)

        assert dto.username == "bob"
        assert dto.email == "bob@test.com"


class TestDTOToDTO:
    """Mapping between two different DTO dataclasses."""

    def test_dto_to_dto_matching_fields(self) -> None:
        source = UserDTO(username="carol", email="carol@test.com")
        mapper = Mapper()

        profile = mapper.map(source, ProfileDTO)

        assert profile.username == "carol"
        assert profile.email == "carol@test.com"
        assert profile.bio == ""  # default kept


class TestCustomFieldMap:
    """Explicit source->dest field name mapping."""

    def test_field_map_renames_field(self) -> None:
        entity = UserEntity(id=1, username="alice", email="a@b.com", active=True)
        mapper = Mapper()
        mapper.add_mapping(
            UserEntity,
            UserResponse,
            field_map={"username": "name", "active": "is_active"},
        )

        resp = mapper.map(entity, UserResponse)

        assert resp.name == "alice"
        assert resp.email == "a@b.com"
        assert resp.is_active is True

    def test_field_map_without_registration_falls_back_to_name_match(self) -> None:
        """Without a registered mapping, only identically named fields match.

        ``UserResponse.name`` is required and has no default, so the mapper
        raises ``TypeError`` when no source field matches.
        """
        entity = UserEntity(id=1, username="alice", email="a@b.com")
        mapper = Mapper()

        # UserResponse expects 'name', but source has 'username' — no match
        with pytest.raises(TypeError):
            mapper.map(entity, UserResponse)


class TestTransformers:
    """Field-level value transformers applied during mapping."""

    def test_transformer_applied_to_field(self) -> None:
        entity = UserEntity(id=1, username="alice", email="a@b.com")
        mapper = Mapper()
        mapper.add_mapping(
            UserEntity,
            UserDTO,
            transformers={"username": str.upper},
        )

        dto = mapper.map(entity, UserDTO)

        assert dto.username == "ALICE"
        assert dto.email == "a@b.com"

    def test_transformer_with_field_map(self) -> None:
        """Transformer keyed by *dest* field name works with field_map."""
        entity = UserEntity(id=1, username="bob", email="b@c.com", active=True)
        mapper = Mapper()
        mapper.add_mapping(
            UserEntity,
            UserResponse,
            field_map={"username": "name", "active": "is_active"},
            transformers={"name": str.upper},
        )

        resp = mapper.map(entity, UserResponse)

        assert resp.name == "BOB"
        assert resp.is_active is True


class TestExclude:
    """Fields in the exclude set are skipped during mapping."""

    def test_excluded_field_uses_dest_default(self) -> None:
        entity = UserEntity(id=1, username="alice", email="a@b.com", active=False)
        mapper = Mapper()
        mapper.add_mapping(
            UserEntity,
            UserResponse,
            field_map={"username": "name", "active": "is_active"},
            exclude={"is_active"},
        )

        resp = mapper.map(entity, UserResponse)

        assert resp.name == "alice"
        assert resp.email == "a@b.com"
        # is_active should keep its dataclass default (True), not source value (False)
        assert resp.is_active is True


class TestMapList:
    """Mapping a list of source objects in one call."""

    def test_map_list_returns_list_of_dest_type(self) -> None:
        entities = [
            UserEntity(id=1, username="alice", email="a@b.com"),
            UserEntity(id=2, username="bob", email="b@c.com"),
        ]
        mapper = Mapper()

        dtos = mapper.map_list(entities, UserDTO)

        assert len(dtos) == 2
        assert all(isinstance(d, UserDTO) for d in dtos)
        assert dtos[0].username == "alice"
        assert dtos[1].username == "bob"

    def test_map_list_empty(self) -> None:
        mapper = Mapper()

        dtos = mapper.map_list([], UserDTO)

        assert dtos == []


class TestPartialMapping:
    """Source has more fields than dest — extras are silently dropped."""

    def test_extra_source_fields_are_ignored(self) -> None:
        entity = UserEntity(id=99, username="x", email="x@y.com", active=False)
        mapper = Mapper()

        dto = mapper.map(entity, UserDTO)

        assert dto.username == "x"
        assert dto.email == "x@y.com"
        # 'id' and 'active' from source are not present on UserDTO — no error
