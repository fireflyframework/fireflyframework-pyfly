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
"""Tests for BaseDocument — uses mongomock for Beanie initialisation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument

# ---------------------------------------------------------------------------
# Test document subclass
# ---------------------------------------------------------------------------


class UserDocument(BaseDocument):
    name: str
    email: str = ""

    class Settings:
        name = "users"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def init_db():
    """Initialise Beanie with an in-memory mock client."""
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[UserDocument])
    yield
    client.close()


# ===========================================================================
# Tests
# ===========================================================================


class TestBaseDocument:
    """BaseDocument audit fields and Pydantic behaviour."""

    def test_subclass_has_audit_fields(self):
        """BaseDocument subclass has created_at, updated_at, created_by, updated_by."""
        fields = set(UserDocument.model_fields)
        assert "created_at" in fields
        assert "updated_at" in fields
        assert "created_by" in fields
        assert "updated_by" in fields

    def test_default_audit_timestamps_set(self):
        """Audit timestamps are set by default."""
        doc = UserDocument(name="Alice")
        assert isinstance(doc.created_at, datetime)
        assert isinstance(doc.updated_at, datetime)

    def test_default_audit_by_fields_none(self):
        """created_by and updated_by default to None."""
        doc = UserDocument(name="Alice")
        assert doc.created_by is None
        assert doc.updated_by is None

    def test_audit_by_fields_can_be_set(self):
        """Audit by fields accept string values."""
        doc = UserDocument(name="Alice", created_by="admin", updated_by="admin")
        assert doc.created_by == "admin"
        assert doc.updated_by == "admin"

    def test_pydantic_model_dump(self):
        """Pydantic serialisation works."""
        doc = UserDocument(name="Alice", email="alice@example.com")
        data = doc.model_dump(exclude={"id", "revision_id"})
        assert data["name"] == "Alice"
        assert data["email"] == "alice@example.com"
        assert "created_at" in data
        assert "updated_at" in data

    def test_pydantic_model_validate(self):
        """Pydantic deserialisation works."""
        now = datetime.now(UTC)
        data = {
            "name": "Bob",
            "email": "bob@example.com",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        doc = UserDocument.model_validate(data)
        assert doc.name == "Bob"
        assert doc.email == "bob@example.com"

    def test_settings_collection_name(self):
        """Subclass Settings.name configures the collection."""
        assert UserDocument.Settings.name == "users"

    def test_inherits_from_beanie_document(self):
        """BaseDocument is a Beanie Document."""
        from beanie import Document

        assert issubclass(BaseDocument, Document)
        assert issubclass(UserDocument, Document)

    def test_subclass_custom_fields(self):
        """Subclass custom fields are accessible."""
        doc = UserDocument(name="Charlie", email="charlie@example.com")
        assert doc.name == "Charlie"
        assert doc.email == "charlie@example.com"
